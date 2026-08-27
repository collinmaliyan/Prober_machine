# เอกสารอธิบายการไหลของข้อมูล (RFID Prober Data Flow Architecture)

เอกสารนี้จัดทำขึ้นเพื่ออธิบายภาพรวม สถาปัตยกรรม และโฟลว์การไหลของข้อมูล (**Data Flow**) ในโครงการ **RFID Prober System** ตั้งแต่ขั้นตอนการเตรียมแท็ก การตรวจสอบความถูกต้องหน้างาน ไปจนถึงการซิงค์ข้อมูลระหว่าง **เครื่อง Prober (Local SQLite)** และ **PC Server (PostgreSQL/MariaDB)**

---

## 1. องค์ประกอบหลักในระบบ (System Entities)

```mermaid
graph TB
    subgraph Central_Layer ["🏢 PC Server (Master Central System)"]
        ServerDB[("PostgreSQL / MariaDB<br/>(Master Database)")]
        MES["ระบบ MES / วิศวกรซ่อมบำรุง<br/>(บันทึกรอบ Touchdown & ประวัติ PM)"]
        MES --> ServerDB
    end

    subgraph Hardware_Prep ["🏷️ การเตรียมอุปกรณ์"]
        Writer["โปรแกรม Write EPC<br/>(RFID Writer Tool)"]
        FPCTag["แผ่น Probe Card (FPC)<br/>(ชิป RFID บันทึกเฉพาะ fpc_id)"]
        HdrTag["แผ่น Header Card<br/>(ชิป RFID บันทึกเฉพาะ header_id)"]
        Writer --> FPCTag
        Writer --> HdrTag
    end

    subgraph Prober_Machine ["⚙️ เครื่องทดสอบ Prober (Edge Device: เช่น AVT#55)"]
        R_HDR["RFID #1: Header Reader<br/>(YRM100 / COM4)"]
        R_FPC["RFID #2: FPC Reader + Sensor<br/>(YRM100 / COM6 + GPIO Pin 6)"]
        R_CASS["RFID #3: Cassette Reader<br/>(OMNIKEY / COM7)"]
      
        App["Python Backend (Main_Prober)<br/>- Data Coordinator & Validation<br/>- Failover & Sync Engine"]
        SQLiteDB[("Local SQLite Database<br/>(Offline Cache สำรองในเครื่อง)")]
        GUI["Web GUI Dashboard<br/>(Port 8001 / Touchscreen HMI)"]

        R_HDR --> App
        R_FPC --> App
        R_CASS --> App
        App <--> SQLiteDB
        App <--> GUI
    end

    ServerDB <-->|1. ดึง Master Data ลง SQLite<br/>2. ส่ง Scan Log กลับขึ้น Server| App

    style Central_Layer fill:#f0fdf4,stroke:#16a34a,stroke-width:2px
    style Hardware_Prep fill:#eff6ff,stroke:#2563eb,stroke-width:2px
    style Prober_Machine fill:#f8fafc,stroke:#475569,stroke-width:2px
```

---

## 2. โฟลว์การทำงาน 4 ขั้นตอน (End-to-End Data Flow)

```mermaid
flowchart TD
    subgraph Step1 ["📌 ขั้นที่ 1: เตรียมการ์ด (Initial Tag Setup)"]
        A1["1.1 วิศวกรลงทะเบียนข้อมูล FPC & Header ใน Master DB<br/>(กำหนดชื่อรุ่น, ลิมิต Touchdown, วัน PM, และคู่ที่อนุญาต)"]
        A2["1.2 ใช้โปรแกรม Write EPC เขียนรหัสลงชิป RFID<br/>- FPC เช่น 'P13080-FHB-0364'<br/>- Header เช่น 'H13080-PHS-11'"]
        A1 --> A2
    end

    subgraph Step2 ["📌 ขั้นที่ 2: เริ่มต้นระบบ (System Startup & Sync)"]
        B1["2.1 เครื่อง Prober เปิดเครื่องและรันโปรแกรม Main_Prober"]
        B2["2.2 โปรแกรมเชื่อมต่อไปยัง PC Server เพื่อดึง Master Data ล่าสุด"]
        B3["2.3 บันทึกสำรองลง Local SQLite ในเครื่อง Prober (Local Cache)"]
        B1 --> B2 --> B3
    end

    subgraph Step3 ["📌 ขั้นที่ 3: ปฏิบัติงานจริงหน้าเครื่อง (Live Scanning & Validation)"]
        C1["3.1 ผู้ปฏิบัติงานเสียบแผ่น FPC และ Header เข้าเครื่อง Prober"]
        C2["3.2 เซนเซอร์ (SensorGate) ตรวจจับการเสียบ -> หัวอ่าน RFID อ่านรหัสจากชิป"]
        C3["3.3 โค้ดนำรหัสไปค้นหาใน Database (is_active_pair & get_enrichment)"]
        C4{"3.4 ตรวจสอบความถูกต้อง"}
      
        D1["🟢 เข้ากันได้ (Match OK)<br/>- แสดงสถานะสีเขียวบน GUI (8001)<br/>- ดึงยอด Touchdown, วัน PM, Lot/Batch มาแสดงครบ"]
        D2["🔴 ไม่เข้ากัน (Mismatch)<br/>- แสดงเตือนสีแดง (Mismatch / Not Allowed)<br/>- แจ้งเตือนผู้ปฏิบัติงานห้ามเริ่มรันงาน"]
      
        C1 --> C2 --> C3 --> C4
        C4 -->|ผ่าน| D1
        C4 -->|ไม่ผ่าน| D2
    end

    subgraph Step4 ["📌 ขั้นที่ 4: บันทึกและส่งรายงาน (Logging & Synchronization)"]
        E1["4.1 ระบบบันทึกประวัติการสแกนลงตาราง scan_log ในเครื่อง"]
        E2["4.2 sync_loop ทยอยส่ง scan_log ขึ้นไปเก็บที่ PC Server ทุก 10 วินาที"]
        E3["4.3 สำรองไฟล์ Daily CSV Log อัตโนมัติทุกเวลา 23:59:00 น."]
        E1 --> E2 --> E3
    end

    Step1 --> Step2 --> Step3
    D1 --> Step4
    D2 --> Step4
```

---

## 3. รายละเอียดการทำงานของแต่ละขั้นตอน

### ขั้นที่ 1: การเตรียมการ์ด (Initial Tag Setup)

* ชิป RFID บนแผ่นการ์ดจะถูกเก็บเฉพาะ **"รหัสบัตรประจำตัว"** สั้นๆ (EPC ASCII):
  * **แผ่น FPC:** บันทึกรหัส `fpc_id` (เช่น `P13080-FHB-0364`)
  * **แผ่น Header:** บันทึกรหัส `header_id` (เช่น `H13080-PHS-11`)
* ข้อมูลเชิงลึกทั้งหมด (Touchdown, วัน PM, ผู้ผลิต, จำนวน Sites, ล็อตการผลิต) จะถูกลงทะเบียนไว้ใน **ฐานข้อมูลส่วนกลาง (PC Server)**

---

### ขั้นที่ 2: การเริ่มต้นระบบและการซิงค์ข้อมูล (Startup Sync)

* เมื่อเปิดเครื่อง Prober ระบบจะดึง Master Data จาก **PC Server (PostgreSQL/MariaDB)** มาเซฟทับลงใน **SQLite ในเครื่อง**
* **ประโยชน์:** เครื่อง Prober จะมีข้อมูลที่อัปเดตล่าสุดอยู่เสมอ และสามารถทำงานต่อได้ 100% ทันทีแม้ในเวลาที่สาย LAN หลุดหรือเซิร์ฟเวอร์หลักปิดปรับปรุง

---

### ขั้นที่ 3: การสแกนและตรวจสอบหน้างาน (Live Scanning & Validation)

1. **สัญญาณเซนเซอร์ (Gated Reading):** เมื่อเสียบแผ่น FPC ขาเซนเซอร์จะส่งสัญญาณให้เปิด Time Window (8–10 วินาที) เพื่ออ่านแท็ก FPC
2. **การจับคู่ (Pair Matching):** ระบบจะนำรหัส `header_id` และ `fpc_id` ไปเทียบกับตาราง `header` และ `fpc_header_allowed`
3. **การดึงข้อมูลประกอบ (Data Enrichment):**
   * ดึง `touchdown`, `latest_pm`, `comment` จากตาราง `fpc`
   * ดึง `batch_id`, `lot_id` จากตาราง `batch`
4. **การแสดงผลบนหน้าจอ HMI Dashboard (พอร์ต 8001):**
   * **กรณีถูกต้อง (Match OK):** แสดงกรอบสีเขียว พร้อม Touchdown Gauge
   * **กรณีผิดคู่ (Mismatch):** แสดงกรอบสีแดง แจ้งเตือนทันทีเพื่อป้องกันหัวเข็มเสียหาย

---

### ขั้นที่ 4: การบันทึกและส่งรายงาน (Logging & Synchronization)

1. **One-Shot Log Insert:** ระบบจะบันทึกประวัติลงตาราง `scan_log` อย่างแม่นยำ 1 ครั้งต่อ 1 รอบการเสียบการ์ด
2. **Background Sync:** เธรด `sync_loop` จะตรวจสอบรายการที่มี `synced = 0` และส่งผ่าน REST API ขึ้นไปยัง PC Server ทุก 10 วินาที
3. **Daily Backup:** เธรด `BackupManager` จะ Export ข้อมูลการสแกนประจำวันออกมาเป็นไฟล์ `.csv` (Excel-compatible) ตอน 23:59:00 น.

---

## 4. ตารางเปรียบเทียบหน้าที่: PC Server vs SQLite ในเครื่อง

| หัวข้อ                       | PC Server (PostgreSQL / MariaDB)                                                                                                                                                                            | Local SQLite (ประจำเครื่อง Prober)                                                                                              |
| :--------------------------------- | :---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------------------ |
| **ตำแหน่ง**           | เซิร์ฟเวอร์ส่วนกลางของโรงงาน (IP: 92.121.78.12)                                                                                                                                 | อยู่ในไฟล์`RFID_database_SQLite.db` ในเครื่อง                                                                          |
| **หน้าที่หลัก**   | **Master Database (ฐานข้อมูลแม่)**                                                                                                                                                        | **Edge Cache & Offline Fallback (ฐานข้อมูลสำรอง)**                                                                      |
| **ความสำคัญ**       | เป็นศูนย์กลางที่เก็บข้อมูล FPC ทุกใบในโรงงาน เพื่อให้เครื่อง Prober ทุกเครื่อง (`AVT55`, `AVT56`, ...) เห็นข้อมูลตรงกัน | ช่วยให้เครื่อง Prober ทำงานได้ต่อเนื่องโดยไม่สะดุดแม้ในยาม Network ขัดข้อง         |
| **ทิศทางข้อมูล** | - จ่าย Master Data (FPC/Header/PM) ลงไปให้เครื่อง Prober- รับ Scan Logs จากเครื่อง Prober มาบันทึกยอดรวม                                                       | - รับ Master Data มาอัปเดตแคชในเครื่อง- ส่ง Scan Logs ที่สแกนได้หน้างานขึ้นไปให้ Server |

---

## 5. แผนผังความสัมพันธ์ของตารางข้อมูล (Database Schema ERD)

```mermaid
erDiagram
    EMPLOYEE {
        string employee_id PK "รหัสพนักงาน/Admin"
    }

    FPC {
        string fpc_id PK "รหัสแท็ก FPC (EPC)"
        string name "ชื่อรุ่น เช่น P13080"
        int touchdown "จำนวนครั้งที่สัมผัสเวเฟอร์"
        datetime latest_pm "วันที่ทำ PM ล่าสุด"
        string comment "บันทึกผลการตรวจสอบ"
        string supplier "ผู้ผลิต"
    }

    HEADER {
        string header_id PK "รหัสแท็ก Header (EPC)"
        string fpc_id FK "รหัส FPC ที่ผูกอยู่ปัจจุบัน"
        string name "ชื่อ Header"
        date date "วันที่ผูกคู่"
    }

    FPC_HEADER_ALLOWED {
        string fpc_id PK,FK "รหัสแท็ก FPC"
        string header_id PK,FK "รหัสแท็ก Header"
    }

    BATCH {
        string batch_id PK "รหัส Batch การผลิต"
        string lot_id "รหัส Lot"
        string fpc_id FK "ชื่อรุ่น FPC เช่น P13080"
    }

    SCAN_LOG {
        int id PK "Auto Increment"
        string source "BOTH / FPC / HDR"
        string header_id "รหัส Header ที่สแกนได้"
        string fpc_id "รหัส FPC ที่สแกนได้"
        string batch_id "รหัส Batch ขณะสแกน"
        string lot_id "รหัส Lot ขณะสแกน"
        int touchdown "ยอด Touchdown ขณะสแกน"
        date latest_pm "วัน PM ขณะสแกน"
        datetime timestamp "วันเวลาที่บันทึก"
        int synced "0=ยังไม่ซิงค์, 1=ซิงค์ขึ้น Server แล้ว"
    }

    FPC ||--o{ FPC_HEADER_ALLOWED : "อนุญาตให้คู่กัน"
    HEADER ||--o{ FPC_HEADER_ALLOWED : "อนุญาตให้คู่กัน"
    HEADER ||--|| FPC : "คู่ Active"
    FPC ||--o{ BATCH : "เชื่อมผ่าน fpc.name"
    FPC ||--o{ SCAN_LOG : "บันทึกประวัติ"
    HEADER ||--o{ SCAN_LOG : "บันทึกประวัติ"
```



---

*เอกสารนี้จัดทำและปรับปรุงล่าสุดเมื่อ: 19 สิงหาคม 2026*
