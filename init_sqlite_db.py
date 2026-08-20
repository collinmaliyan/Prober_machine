import sqlite3
import os

DB_FILE = "RFID_database_SQLite.db"

def init_sqlite_db():
    if os.path.exists(DB_FILE):
        os.remove(DB_FILE)
        print(f"[SQLITE] Existing {DB_FILE} removed for fresh build.")

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # 1. employee table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS employee (
        employee_id TEXT PRIMARY KEY
    );
    """)
    cursor.executemany("INSERT INTO employee (employee_id) VALUES (?);", [
        ('13989336',), ('13989472',), ('ADMIN',)
    ])

    # 1.5. batch table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS batch (
        batch_id TEXT PRIMARY KEY,
        lot_id TEXT,
        fpc_id TEXT,
        product_code TEXT
    );
    """)
    batch_rows = [
        ('BATCH-001', 'LOT-666', 'P13080', 'PRD-13080'),
        ('BATCH-111', 'LOT-222', '2ID031', 'PRD-2ID031'),
        ('BBATLP111', 'K0SD23124000', 'P15230', 'PRD-15230')
    ]
    cursor.executemany("INSERT INTO batch VALUES (?,?,?,?);", batch_rows)

    # 2. fpc table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS fpc (
        fpc_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        latest_pm DATETIME,
        number INTEGER NOT NULL,
        location TEXT NOT NULL,
        sites INTEGER NOT NULL,
        touchdown INTEGER NOT NULL,
        test_system TEXT NOT NULL,
        supplier TEXT NOT NULL,
        source TEXT NOT NULL,
        comment TEXT
    );
    """)
    fpc_rows = [
        ('2ID018FV002B', '2ID018', '2025-10-10 11:22:37', 2, 'PS201', 32, 6500, 'EWFM', 'Feinmetal', 'FV', 'comment'),
        ('2ID018FV003B', '2ID018', '2025-09-29 16:44:12', 3, '216', 32, 4000, 'EWFM', 'Feinmetal', 'FV', 'comment'),
        ('2ID018FV004B', '2ID018', '2025-09-29 16:46:17', 4, '375', 32, 5000, 'EWFM', 'Feinmetal', 'FV', 'comment'),
        ('2ID031FV001B', '2ID031', '2025-09-29 16:48:39', 1, 'PS255', 32, 6000, 'EWFM', 'Feimetal', 'FV', 'comment'),
        ('2ID031FV002B', '2ID031', '2025-09-29 16:53:21', 2, 'PS327', 32, 7000, 'EWFM', 'Feinmtal', 'FV', 'comment'),
        ('2ID031FV003B', '2ID031', '2025-10-10 11:11:04', 3, 'PS323', 32, 5500, 'EWFM', 'Feimetal', 'FV', 'comment'),
        ('P13080-FHB-0364', 'P13080', '2025-07-12 16:00:00', 364, 'PS47', 192, 62000, 'EWFM', 'Feinmetal', 'FHB', 'CLEAN ; ; Testfile No PM PASS/OFF LINE CLEANING/ QC PASS H=322UM/CPASS/20090'),
        ('P15230-FHH-2362', 'P15230', '2025-03-25 11:00:00', 2362, 'PS34', 1200, 59000, 'EWFM', 'Feinmetal', 'FHH', 'CLEAN ; PRVX3_06 ; Testfile No CPT_0007 DIA32/19929')
    ]
    cursor.executemany("INSERT INTO fpc VALUES (?,?,?,?,?,?,?,?,?,?,?);", fpc_rows)

    # 3. header table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS header (
        header_id TEXT PRIMARY KEY,
        fpc_id TEXT,
        needle TEXT NOT NULL,
        name TEXT NOT NULL,
        number INTEGER NOT NULL,
        date DATE NOT NULL,
        FOREIGN KEY (fpc_id) REFERENCES fpc (fpc_id)
    );
    """)
    header_rows = [
        ('2ID018FV002B', '2ID018FV002B', 'S', 'W41H25S2P', 3, '2020-10-23'),
        ('2ID018FV003B', '2ID018FV003B', 'S', 'W41H25S2P', 2, '2020-03-25'),
        ('2ID018FV004B', '2ID018FV004B', 'S', 'W41H25S2P', 4, '2025-07-14'),
        ('2ID031FV001B', '2ID031FV001B', 'S', 'W41H25S2P', 4, '2025-07-14'),
        ('2ID031FV002B', '2ID031FV002B', 'S', 'W41H25S2P', 2, '2021-11-15'),
        ('2ID031FV003B', '2ID031FV003B', 'S', 'W41H25S2P', 1, '2021-11-19'),
        ('H13080-PHS-11', 'P13080-FHB-0364', 'S', 'W43H25S2P', 11, '2025-07-30'),
        ('H15230-PHS-02', None, 'S', 'D09H25S2P', 2, '2021-06-10'),
        ('H15230-PHS-03', 'P15230-FHH-2362', 'S', 'D09H25S2P', 3, '2025-09-26')
    ]
    cursor.executemany("INSERT INTO header VALUES (?,?,?,?,?,?);", header_rows)

    # 4. fpc_header_allowed table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS fpc_header_allowed (
        fpc_id TEXT NOT NULL,
        header_id TEXT NOT NULL,
        PRIMARY KEY (fpc_id, header_id)
    );
    """)
    cursor.executemany("INSERT INTO fpc_header_allowed VALUES (?,?);", [
        ('P13080-FHB-0364', 'H13080-PHS-11'),
        ('P15230-FHH-2362', 'H15230-PHS-02'),
        ('P15230-FHH-2362', 'H15230-PHS-03')
    ])

    # 5. scan_log table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS scan_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL,
        header_id TEXT,
        header_name TEXT,
        fpc_id TEXT,
        batch_id TEXT,
        lot_id TEXT,
        touchdown INTEGER,
        latest_pm DATE,
        comment TEXT,
        agv_no TEXT,
        machine_no TEXT,
        timestamp DATETIME NOT NULL,
        synced INTEGER NOT NULL DEFAULT 0
    );
    """)
    scan_log_rows = [
        (1, 'BOTH', 'H15230-PHS-03', None, 'P15230-FHH-2362', 'BBATLP111', 'K0SD23124000', 59000, '2025-03-25', 'CLEAN ; PRVX3_06 ; Testfile No CPT_0007 DIA32/19929', '-', 'AVT#55', '2025-10-08 10:51:33', 0),
        (2, 'BOTH', 'H15230-PHS-03', None, 'P15230-FHH-2362', 'xxx_2', 'xxx', 59000, '2025-03-25', 'CLEAN ; PRVX3_06 ; Testfile No CPT_0007 DIA32/19929', '-', 'AVT#55', '2025-10-08 10:54:05', 0),
        (3, 'BOTH', 'H15230-PHS-02', None, 'P15230-FHH-2362', None, None, None, None, None, '-', 'AVT#55', '2025-10-08 10:54:25', 0),
        (4, 'BOTH', 'H15230-PHS-02', None, 'P15230-FHH-2362', None, None, None, None, None, '-', 'AVT#55', '2025-10-08 10:57:31', 0),
        (5, 'BOTH', 'H15230-PHS-03', None, 'P15230-FHH-2362', None, None, 59000, '2025-03-25', 'CLEAN ; PRVX3_06 ; Testfile No CPT_0007 DIA32/19929', '-', 'AVT#55', '2025-10-08 10:58:04', 0),
        (6, 'BOTH', 'H15230-PHS-03', None, 'P15230-FHH-2362', None, None, 59000, '2025-03-25', 'CLEAN ; PRVX3_06 ; Testfile No CPT_0007 DIA32/19929', '-', 'AVT#55', '2025-10-08 11:12:44', 0),
        (7, 'BOTH', 'H15230-PHS-03', None, 'P15230-FHH-2362', None, None, 59000, '2025-03-25', 'CLEAN ; PRVX3_06 ; Testfile No CPT_0007 DIA32/19929', '-', 'AVT#55', '2025-10-08 11:14:07', 0),
        (8, 'BOTH', 'H15230-PHS-03', None, 'P15230-FHH-2362', None, None, 59000, '2025-03-25', 'CLEAN ; PRVX3_06 ; Testfile No CPT_0007 DIA32/19929', '-', 'AVT#55', '2025-10-08 11:33:43', 0),
        (9, 'BOTH', 'H15230-PHS-03', None, 'P15230-FHH-2362', None, None, 59000, '2025-03-25', 'CLEAN ; PRVX3_06 ; Testfile No CPT_0007 DIA32/19929', '-', 'AVT#55', '2025-10-08 11:39:05', 0)
    ]
    cursor.executemany("INSERT INTO scan_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?);", scan_log_rows)

    # 6. cassette table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS cassette (
        cassette_id TEXT PRIMARY KEY,
        machine_status TEXT DEFAULT 'Active',
        lot_id TEXT,
        batch_id TEXT,
        last_cleaning DATETIME,
        next_cleaning DATETIME
    );
    """)
    cassette_rows = [
        ('CASS-001', 'Active', 'LOT-CASS-001', 'BATCH-CASS-001', '2025-10-01 08:00:00', '2025-10-15 08:00:00'),
        ('CASS-002', 'Active', 'LOT-CASS-002', 'BATCH-CASS-002', '2025-10-02 09:30:00', '2025-10-16 09:30:00')
    ]
    cursor.executemany("INSERT INTO cassette VALUES (?,?,?,?,?,?);", cassette_rows)

    # 7. cassette_reader_log table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS cassette_reader_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cassette_id TEXT NOT NULL,
        machine_status TEXT,
        lot_id TEXT,
        batch_id TEXT,
        last_cleaning DATETIME,
        next_cleaning DATETIME,
        machine_no TEXT,
        timestamp DATETIME NOT NULL,
        synced INTEGER NOT NULL DEFAULT 0
    );
    """)

    # 8. header_reader_log table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS header_reader_log (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        header_id TEXT NOT NULL,
        machine_no TEXT,
        timestamp DATETIME NOT NULL,
        synced INTEGER NOT NULL DEFAULT 0
    );
    """)

    # 9. fpc_reader_log table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS fpc_reader_log (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        fpc_id TEXT NOT NULL,
        agv_no TEXT,
        timestamp DATETIME NOT NULL,
        synced INTEGER NOT NULL DEFAULT 0
    );
    """)

    # 8. system_log table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS system_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_id TEXT,
        action TEXT NOT NULL,
        ip TEXT,
        ts DATETIME NOT NULL,
        FOREIGN KEY (employee_id) REFERENCES employee (employee_id)
    );
    """)
    system_log_rows = [
        (1, 'ADMIN', 'login', '127.0.0.1', '2025-08-14 15:33:05'),
        (2, 'ADMIN', 'logout', '127.0.0.1', '2025-08-14 15:33:09'),
        (3, '13989472', 'login', '127.0.0.1', '2025-08-14 15:33:12'),
        (4, 'ADMIN', 'login', '127.0.0.1', '2025-08-15 09:47:41'),
        (5, 'ADMIN', 'reset_ip_address', '127.0.0.1', '2025-08-15 09:47:52'),
        (6, 'ADMIN', 'logout', '127.0.0.1', '2025-08-15 09:48:00'),
        (7, 'ADMIN', 'login', '127.0.0.1', '2025-08-15 11:38:29'),
        (8, 'ADMIN', 'logout', '127.0.0.1', '2025-08-15 11:38:34'),
        (9, 'ADMIN', 'login', '127.0.0.1', '2025-08-15 11:39:31'),
        (10, 'ADMIN', 'logout', '127.0.0.1', '2025-08-15 11:39:35'),
        (11, 'ADMIN', 'login', '127.0.0.1', '2025-08-15 15:30:47'),
        (12, 'ADMIN', 'login', '127.0.0.1', '2025-08-19 09:42:15'),
        (13, 'ADMIN', 'login', '127.0.0.1', '2025-08-28 11:10:42'),
        (14, 'ADMIN', 'login', '127.0.0.1', '2025-08-28 11:12:34'),
        (15, 'ADMIN', 'login', '127.0.0.1', '2025-08-28 11:15:13'),
        (16, 'ADMIN', 'login', '127.0.0.1', '2025-08-28 11:15:31'),
        (17, 'ADMIN', 'login', '127.0.0.1', '2025-09-19 00:05:11'),
        (18, 'ADMIN', 'login', '127.0.0.1', '2025-09-19 00:05:46'),
        (19, 'ADMIN', 'login', '127.0.0.1', '2025-10-01 11:08:42'),
        (20, 'ADMIN', 'login', '127.0.0.1', '2025-10-01 11:09:34'),
        (21, '13989472', 'login', '127.0.0.1', '2025-10-01 11:20:05'),
        (22, '13989472', 'reset_ip_address', '127.0.0.1', '2025-10-01 11:20:11'),
        (23, '13989472', 'reset_ip_address', '127.0.0.1', '2025-10-01 11:20:20'),
        (24, '13989472', 'logout', '127.0.0.1', '2025-10-01 11:20:24'),
        (25, 'ADMIN', 'login', '127.0.0.1', '2025-10-01 11:20:26'),
        (26, 'ADMIN', 'logout', '127.0.0.1', '2025-10-01 11:20:30'),
        (27, 'ADMIN', 'login', '127.0.0.1', '2025-10-08 00:54:57'),
        (28, '13989472', 'login', '127.0.0.1', '2025-10-08 00:55:06'),
        (29, 'ADMIN', 'login', '127.0.0.1', '2025-10-08 08:39:38'),
        (30, 'ADMIN', 'logout', '127.0.0.1', '2025-10-08 08:39:41')
    ]
    cursor.executemany("INSERT INTO system_log VALUES (?,?,?,?,?);", system_log_rows)

    conn.commit()
    conn.close()
    print(f"[SQLITE SUCCESS] SQLite database successfully created at: {os.path.abspath(DB_FILE)}")

if __name__ == "__main__":
    init_sqlite_db()
