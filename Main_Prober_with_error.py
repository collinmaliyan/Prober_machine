import csv
import os
import json
import time
import glob
import platform
import socket
import threading
import base64, math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from serial.tools import list_ports
import serial
import mysql.connector
import requests
from requests.auth import HTTPBasicAuth 
from flask import Flask, request, jsonify, send_from_directory, session, render_template
import flask
from flask_cors import CORS
from functools import wraps
from config import Config, ROBOTS, _headers, HTTP_TIMEOUT

def fetch_pose(robot: dict) -> dict:
    url = f"{robot['base'].rstrip('/')}/status"
    try:
        r = requests.get(url, headers=_headers(robot["user"], robot["pass"]), timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        js = r.json()
        x = float(js["position"]["x"])
        y = float(js["position"]["y"])
        theta_deg = float(js["position"].get("orientation", 0.0))
        theta = math.radians(theta_deg)
        return {"name": robot["name"], "color": robot["color"], "x": x, "y": y, "theta": theta,
                "map_id": js.get("map_id"), "ok": True}
    except Exception as e:
        return {"name": robot["name"], "color": robot["color"], "ok": False, "error": str(e)}


# =============================================================================
# DATABASE UTILITIES
# =============================================================================

class SQLiteCursorWrapper:
    def __init__(self, sqlite_cursor, as_dict=False):
        self.cursor = sqlite_cursor
        self.as_dict = as_dict

    def execute(self, sql, params=()):
        sql_converted = sql.replace("%s", "?")
        if params is None: params = ()
        return self.cursor.execute(sql_converted, params)

    def fetchone(self):
        row = self.cursor.fetchone()
        if row is None or not self.as_dict:
            return row
        cols = [col[0] for col in self.cursor.description]
        return dict(zip(cols, row))

    def fetchall(self):
        rows = self.cursor.fetchall()
        if not self.as_dict:
            return rows
        cols = [col[0] for col in self.cursor.description]
        return [dict(zip(cols, r)) for r in rows]

    def close(self):
        self.cursor.close()

class SQLiteConnWrapper:
    def __init__(self, sqlite_conn):
        self.conn = sqlite_conn

    def cursor(self, dictionary=False):
        return SQLiteCursorWrapper(self.conn.cursor(), as_dict=dictionary)

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()

class DatabaseManager:
    """Handles database connections and operations"""
    
    _MYSQL_ONLINE = None
    _LAST_MYSQL_CHECK = 0

    @staticmethod
    def is_mysql_available():
        now = time.time()
        if DatabaseManager._MYSQL_ONLINE is not None and (now - DatabaseManager._LAST_MYSQL_CHECK < 10.0):
            return DatabaseManager._MYSQL_ONLINE

        host = Config.DB_CONFIG.get('host', 'localhost')
        port = int(Config.DB_CONFIG.get('port', 3306))
        try:
            s = socket.create_connection((host, port), timeout=0.5)
            s.close()
            DatabaseManager._MYSQL_ONLINE = True
        except Exception:
            DatabaseManager._MYSQL_ONLINE = False
            
        DatabaseManager._LAST_MYSQL_CHECK = now
        return DatabaseManager._MYSQL_ONLINE

    @staticmethod
    def get_local_connection():
        """Get local SQLite connection for internal operational logs (scan_log, system_log, etc.)"""
        import sqlite3
        for db_name in ["RFID_database_SQLite.db", "rfid_proj.db"]:
            db_path = os.path.join(os.path.dirname(__file__), db_name)
            if os.path.exists(db_path):
                return SQLiteConnWrapper(sqlite3.connect(db_path))
        raise Exception("No valid local SQLite database available")

    @staticmethod
    def get_store_connection():
        """Get central MySQL connection for Store Verification (fallback to local SQLite cache)"""
        if DatabaseManager.is_mysql_available():
            try:
                return mysql.connector.connect(**Config.DB_CONFIG)
            except Exception as e:
                print(f"[STORE DB WARN] MySQL connect failed: {e}, using SQLite cache fallback")
                DatabaseManager._MYSQL_ONLINE = False
        return DatabaseManager.get_local_connection()

    @staticmethod
    def get_connection():
        """Get database connection (defaults to local SQLite for operational logs)"""
        return DatabaseManager.get_local_connection()
    
    @staticmethod
    def store_fpc_log(fpc_id, timestamp):
        """Insert into fpc_reader_log with synced=0"""
        try:
            conn = DatabaseManager.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO fpc_reader_log (fpc_id, agv_no, timestamp, synced)
                VALUES (%s, %s, %s, %s)
            """, (fpc_id, Config.AGV_NO, timestamp, 0))
            conn.commit()
            conn.close()
            print(f"[FPC LOG STORED] {fpc_id} at {timestamp}")
            return True
        except Exception as e:
            print(f"[ERROR] DB insert (fpc_reader_log): {e}")
            return False


    @staticmethod
    def store_agv_log(fpc_id, timestamp, header_id):
        """Insert into agv_reader_log matching new column order/names"""
        try:
            conn = DatabaseManager.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO agv_reader_log (FPC_id, Timestamp, AGV_No, Header_id, Machine_No)
                VALUES (%s, %s, %s, %s, %s)
            """, (fpc_id, timestamp, Config.AGV_NO, header_id, Config.MACHINE_NO))
            conn.commit()
            conn.close()
            print(f"[LOG STORED] {fpc_id} with Header {header_id} at {timestamp}")
            return True
        except Exception as e:
            print(f"[ERROR] DB insert: {e}")
            return False

    
    @staticmethod
    def get_fpc_details(fpc_id):
        """Get FPC details from new schema"""
        try:
            conn = DatabaseManager.get_connection()
            cursor = conn.cursor()
            # columns in new schema: touchdown, latest_pm, comment
            cursor.execute("""
                SELECT touchdown, latest_pm, comment
                FROM fpc
                WHERE fpc_id = %s
            """, (fpc_id,))
            row = cursor.fetchone()
            conn.close()
            return row  # (touchdown, latest_pm, comment)
        except Exception as e:
            print(f"[ERROR] get_fpc_details: {e}")
            return None
    
    @staticmethod
    def get_header_id(fpc_id):
        """Newest header for this FPC (by date)"""
        try:
            conn = DatabaseManager.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT header_id
                FROM header
                WHERE fpc_id = %s
                ORDER BY date DESC
                LIMIT 1
            """, (fpc_id,))
            row = cursor.fetchone()
            conn.close()
            return row[0] if row else None
        except Exception as e:
            print(f"[ERROR] get_header_id: {e}")
            return None

    
    @staticmethod
    def store_system_log(employee_id, action, ip=None, ts=None):
        """Insert a row into system_log."""
        try:
            conn = DatabaseManager.get_connection()
            cursor = conn.cursor()
            if ts is None:
                ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute('''
                INSERT INTO system_log (employee_id, action, ts, ip)
                VALUES (%s, %s, %s, %s)
            ''', (employee_id, action, ts, ip))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"[ERROR] system_log insert: {e}")
            return False

    @staticmethod
    def get_system_logs(page, page_size, employee_id=None, action=None, date=None):
        """Paged fetch with optional filters."""
        try:
            conn = DatabaseManager.get_connection()
            cursor = conn.cursor()
            filters = []
            params = []

            if employee_id and str(employee_id).strip():
                filters.append("employee_id LIKE %s")
                params.append(f"%{str(employee_id).strip()}%")
            if action and str(action).strip():
                filters.append("action LIKE %s")
                params.append(f"%{str(action).strip()}%")
            if date and str(date).strip():
                filters.append("DATE(ts) = %s")
                params.append(str(date).strip())

            where_clause = "WHERE " + " AND ".join(filters) if filters else ""
            offset = (page - 1) * page_size

            count_sql = f"SELECT COUNT(*) FROM system_log {where_clause}"
            cursor.execute(count_sql, tuple(params))
            total = cursor.fetchone()[0]

            sql = f'''
                SELECT id, employee_id, action, ts, ip
                FROM system_log
                {where_clause}
                ORDER BY ts DESC
                LIMIT %s OFFSET %s
            '''
            cursor.execute(sql, tuple(params + [page_size, offset]))
            rows = cursor.fetchall()
            conn.close()

            logs = [{
                "id": r[0],
                "employeeId": r[1],
                "action": r[2],
                "timestamp": r[3].strftime("%Y-%m-%d %H:%M:%S") if hasattr(r[3], 'strftime') else str(r[3]),
                "ip": r[4]
            } for r in rows]

            pages = (total + page_size - 1) // page_size
            return {"logs": logs, "total": total, "pages": pages}
        except Exception as e:
            print(f"[ERROR] get_system_logs: {e}")
            return {"logs": [], "total": 0, "pages": 0}

    @staticmethod
    def is_valid_employee(employee_id):
        """Validate against 'employee' table (not 'employees')"""
        try:
            conn = DatabaseManager.get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM employee WHERE employee_id = %s", (employee_id,))
            exists = cursor.fetchone()[0] > 0
            conn.close()
            return exists
        except Exception as e:
            print(f"[ERROR] Checking employee: {e}")
            return False

        

    @staticmethod
    def store_header_log(header_id, timestamp):
        """Insert into header_reader_log with synced=0"""
        try:
            conn = DatabaseManager.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO header_reader_log (header_id, machine_no, timestamp, synced)
                VALUES (%s, %s, %s, %s)
            """, (header_id, Config.MACHINE_NO, timestamp, 0))
            conn.commit()
            conn.close()
            print(f"[HDR LOG STORED] {header_id} at {timestamp}")
            return True
        except Exception as e:
            print(f"[ERROR] DB insert (header): {e}")
            return False


    @staticmethod
    def get_fpc_summary(fpc_id: str):
        """Return touchdown, latest_pm, comment from fpc"""
        try:
            conn = DatabaseManager.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT touchdown, latest_pm, comment
                FROM fpc
                WHERE fpc_id = %s
            """, (fpc_id,))
            row = cursor.fetchone()
            conn.close()
            if not row:
                return None
            # (touchdown BIGINT, latest_pm DATETIME/TEXT, comment VARCHAR)
            td, pm, cmt = row
            pm_formatted = None
            if pm:
                if hasattr(pm, 'strftime'):
                    pm_formatted = pm.strftime('%Y-%m-%d')
                else:
                    pm_formatted = str(pm).split(' ')[0]

            return {
                "touchdown": td,
                "pm_date": pm_formatted,
                "comment": cmt
            }
        except Exception as e:
            print(f"[ERROR] get_fpc_summary: {e}")
            return None

    @staticmethod
    def get_batch_info_by_fpc(fpc_id: str):
        """
        Return {'batch_id':..., 'lot_id':...} for a given fpc_id.
        Your batch.fpc_id actually stores the FPC *name* (e.g., 'P13080'),
        while fpc.fpc_id is like 'P13080-FHB-0364'. So join via fpc.name.
        """
        try:
            conn = DatabaseManager.get_connection()
            cursor = conn.cursor()

            # First try direct match (in case future data uses full fpc_id)
            cursor.execute("""
                SELECT batch_id, lot_id FROM batch WHERE fpc_id = %s LIMIT 1
            """, (fpc_id,))
            row = cursor.fetchone()
            if row:
                conn.close()
                return {'batch_id': row[0], 'lot_id': row[1]}

            # Fallback: join through fpc.name
            cursor.execute("""
                SELECT b.batch_id, b.lot_id
                FROM batch b
                JOIN fpc f ON b.fpc_id = f.name
                WHERE f.fpc_id = %s
                LIMIT 1
            """, (fpc_id,))
            row = cursor.fetchone()
            conn.close()
            if row:
                return {'batch_id': row[0], 'lot_id': row[1]}
            return None
        except Exception as e:
            print(f"[ERROR] get_batch_info_by_fpc: {e}")
            return None

    @staticmethod
    def get_fpc_name_by_id(fpc_id: str):
        if not fpc_id:
            return None
        try:
            conn = DatabaseManager.get_connection()
            cur = conn.cursor()
            cur.execute("SELECT name FROM fpc WHERE fpc_id=%s", (fpc_id,))
            row = cur.fetchone()
            conn.close()
            return row[0] if row else None
        except Exception as e:
            print(f"[ERROR] get_fpc_name_by_id: {e}")
            return None

    @staticmethod
    def store_scan_log(timestamp, machine_no, agv_no, fpc_id,
                       header_id=None, header_name=None,
                       batch_id=None, lot_id=None, source='BOTH',
                       touchdown=None, latest_pm=None, comment=None):
        """Write one immutable snapshot row into local SQLite scan_log used by GUI."""
        try:
            conn = DatabaseManager.get_local_connection()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO scan_log
                    (source, header_id, header_name, fpc_id,
                    batch_id, lot_id, touchdown, latest_pm, comment,
                    agv_no, machine_no, timestamp, synced)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0)
            """, (source, header_id, header_name, fpc_id,
                  batch_id, lot_id, touchdown, latest_pm, comment,
                  agv_no, machine_no, timestamp))
            conn.commit()
            conn.close()
            print(f"[SCAN LOG STORED] FPC:{fpc_id} + HDR:{header_id} at {timestamp} (TD:{touchdown}, PM:{latest_pm})")
            return True
        except Exception as e:
            print(f"[ERROR] store_scan_log: {e}")
            return False

    # =============================================================================
    # 🔍 CONFIRM DATA SECTION (SMART STORE PROBE CARD & CABINET VERIFICATION)
    # ส่วนการตรวจสอบความถูกต้องของข้อมูล Tag จากตาราง smart_store_probe_card และ smart_store_cabinet
    # =============================================================================

    @staticmethod
    def is_known_fpc_tag(tag: str) -> bool:
        """[CONFIRM DATA] ตรวจสอบว่า Tag นี้เป็น FPC Tag ในระบบหรือไม่ (เพื่อกัน RF ข้ามไปเข้า Header Reader)"""
        if not tag:
            return False
        clean = str(tag).strip()
        # Fast prefix check: standard FPC tags start with 2ID, P, etc.
        if clean.upper().startswith(('2ID', 'P13', 'P15', 'FPC')):
            return True
        try:
            conn = DatabaseManager.get_store_connection()
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM smart_store_probe_card WHERE LOWER(TRIM(fpc_id)) = LOWER(TRIM(%s)) LIMIT 1", (clean,))
            found = cur.fetchone() is not None
            cur.close()
            conn.close()
            return found
        except Exception:
            return False

    @staticmethod
    def is_known_header_tag(tag: str) -> bool:
        """[CONFIRM DATA] ตรวจสอบว่า Tag นี้เป็น Header Tag ในระบบหรือไม่"""
        if not tag:
            return False
        clean = str(tag).strip()
        # Fast prefix check: standard Header tags start with HD, H1, HDR
        if clean.upper().startswith(('HD', 'H1', 'HDR')):
            return True
        try:
            conn = DatabaseManager.get_store_connection()
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM smart_store_probe_card WHERE LOWER(TRIM(header_id)) = LOWER(TRIM(%s)) LIMIT 1", (clean,))
            found = cur.fetchone() is not None
            cur.close()
            conn.close()
            return found
        except Exception:
            return False

    @staticmethod
    def confirm_probe_card_data(fpc_id: str = None, header_id: str = None) -> dict:
        """
        [CONFIRM DATA] ตรวจสอบความถูกต้องของ FPC Tag และ Header Tag จากตาราง smart_store_probe_card
        
        ผลลัพธ์:
        1. MATCH_OK (🟢 ถูกต้องตรงคู่กัน 100% - อยู่ในบรรทัดเดียวกัน):
           - pair_ok = True, mismatch_detected = False
           - ส่งคืน touchdown, pm_date, comment เพื่อแสดงผลในช่อง WT-Lot Info
        2. MISMATCH (🔴 ผิดคู่ - พบในระบบทั้งคู่แต่อยู่คนละบรรทัด):
           - pair_ok = False, mismatch_detected = True, mismatch_type = 'mismatch'
           - ส่งคืนข้อความเตือนระบุ FPC / Header ที่ถูกต้อง และที่สแกนได้
        3. NOT_FOUND (🟡 ไม่พบในฐานข้อมูล Store):
           - pair_ok = False, mismatch_detected = True, mismatch_type = 'not_found'
           - ส่งคืนข้อความเตือนให้ทำการ Register / Mapping ที่ตู้ Store ก่อน
        """
        clean_fpc = (fpc_id or "").strip()
        clean_header = (header_id or "").strip()

        if not clean_fpc and not clean_header:
            return {
                "status": "EMPTY",
                "pair_ok": None,
                "mismatch_detected": False,
                "mismatch_type": None,
                "mismatch_message": None,
                "fpc_id": None,
                "header_id": None,
                "touchdown": None,
                "pm_date": None,
                "comment": None
            }

        try:
            conn = DatabaseManager.get_store_connection()
            cur = conn.cursor(dictionary=True)

            row_by_header = None
            row_by_fpc = None

            # 1. ค้นหาแถวข้อมูลจาก Header ID
            if clean_header:
                cur.execute("""
                    SELECT fpc_id, header_id, touchdown, latest_pm, comment
                    FROM smart_store_probe_card
                    WHERE LOWER(TRIM(header_id)) = LOWER(TRIM(%s))
                    LIMIT 1
                """, (clean_header,))
                row_by_header = cur.fetchone()

            # 2. ค้นหาแถวข้อมูลจาก FPC ID
            if clean_fpc:
                cur.execute("""
                    SELECT fpc_id, header_id, touchdown, latest_pm, comment
                    FROM smart_store_probe_card
                    WHERE LOWER(TRIM(fpc_id)) = LOWER(TRIM(%s))
                    LIMIT 1
                """, (clean_fpc,))
                row_by_fpc = cur.fetchone()

            cur.close()
            conn.close()

            # ซิงค์ข้อมูลลง SQLite แคชท้องถิ่นอัตโนมัติ เพื่อรองรับการทำงานออฟไลน์
            try:
                sq_conn = DatabaseManager.get_local_connection()
                sq_cur = sq_conn.cursor()
                for r_data in [row_by_header, row_by_fpc]:
                    if r_data:
                        sq_cur.execute("""
                            INSERT OR REPLACE INTO smart_store_probe_card (fpc_id, header_id, touchdown, latest_pm, comment)
                            VALUES (?, ?, ?, ?, ?)
                        """, (r_data.get('fpc_id'), r_data.get('header_id'), r_data.get('touchdown'), str(r_data.get('latest_pm')) if r_data.get('latest_pm') else None, r_data.get('comment')))
                sq_conn.commit()
                sq_conn.close()
            except Exception:
                pass

            # =========================================================================
            # กรณีที่ 1: สแกนครบทั้งสองใบ (Both Header and FPC Present)
            # =========================================================================
            if clean_header and clean_fpc:
                # 1.1 ไม่มีทั้งคู่ใน Database
                if not row_by_header and not row_by_fpc:
                    msg = f"ทั้ง Header ({clean_header}) และ FPC ({clean_fpc}) ไม่พบข้อมูลในระบบ Smart Store หรือยังไม่ได้ลงทะเบียนตู้ Store"
                    print(f"[CONFIRM DATA] NOT FOUND: {msg}")
                    return {
                        "status": "NOT_FOUND",
                        "pair_ok": False,
                        "mismatch_detected": True,
                        "mismatch_type": "not_found",
                        "mismatch_message": msg,
                        "fpc_id": clean_fpc,
                        "header_id": clean_header,
                        "touchdown": None,
                        "pm_date": None,
                        "comment": None
                    }

                # 1.2 Header ไม่มีใน Database
                if not row_by_header:
                    expected_h = (row_by_fpc.get('header_id') or '').strip() if row_by_fpc else ''
                    msg = f"Tag Header ({clean_header}) ไม่พบข้อมูลในระบบ Smart Store หรือยังไม่ได้ลงทะเบียนตู้ Store"
                    if expected_h:
                        msg += f" (ใน Store การ์ด FPC {clean_fpc} กำหนดให้คู่กับ: {expected_h})"
                    print(f"[CONFIRM DATA] NOT FOUND: {msg}")
                    return {
                        "status": "NOT_FOUND",
                        "pair_ok": False,
                        "mismatch_detected": True,
                        "mismatch_type": "not_found",
                        "mismatch_message": msg,
                        "expected_header": expected_h,
                        "scanned_header": clean_header,
                        "fpc_id": clean_fpc,
                        "header_id": clean_header,
                        "touchdown": None,
                        "pm_date": None,
                        "comment": None
                    }

                # 1.3 FPC ไม่มีใน Database
                if not row_by_fpc:
                    expected_f = (row_by_header.get('fpc_id') or '').strip() if row_by_header else ''
                    msg = f"Tag FPC ({clean_fpc}) ไม่พบข้อมูลในระบบ Smart Store หรือยังไม่ได้ทำ Data Mapping จากตู้ Store"
                    if expected_f:
                        msg += f" (ใน Store กำหนดให้ Header {clean_header} คู่กับ FPC: {expected_f})"
                    print(f"[CONFIRM DATA] NOT FOUND: {msg}")
                    return {
                        "status": "NOT_FOUND",
                        "pair_ok": False,
                        "mismatch_detected": True,
                        "mismatch_type": "not_found",
                        "mismatch_message": msg,
                        "expected_fpc": expected_f,
                        "scanned_fpc": clean_fpc,
                        "fpc_id": clean_fpc,
                        "header_id": clean_header,
                        "touchdown": None,
                        "pm_date": None,
                        "comment": None
                    }

                # 1.4 และ 1.5: มีใน Database ทั้งคู่!
                expected_fpc_for_header = (row_by_header.get('fpc_id') or '').strip()
                expected_header_for_fpc = (row_by_fpc.get('header_id') or '').strip()

                if expected_fpc_for_header.lower() == clean_fpc.lower():
                    # อยู่ในบรรทัดเดียวกัน -> MATCH OK! 🟢
                    td = row_by_header.get('touchdown')
                    pm = str(row_by_header.get('latest_pm')) if row_by_header.get('latest_pm') else None
                    comm = row_by_header.get('comment')
                    print(f"[CONFIRM DATA] MATCH OK: Header '{clean_header}' <-> FPC '{clean_fpc}' (TD: {td}, PM: {pm})")
                    return {
                        "status": "MATCH_OK",
                        "pair_ok": True,
                        "mismatch_detected": False,
                        "mismatch_type": None,
                        "mismatch_message": None,
                        "fpc_id": clean_fpc,
                        "header_id": clean_header,
                        "touchdown": td,
                        "pm_date": pm,
                        "comment": comm
                    }
                else:
                    # อยู่คนละบรรทัดกัน -> MISMATCH! 🔴 (Strictly locked to mismatch)
                    msg = f"Header ไม่ตรงคู่ (ใน Store กำหนดให้ Header {clean_header} คู่กับ FPC: {expected_fpc_for_header} แต่การ์ด FPC ที่อ่านได้คือ: {clean_fpc})"
                    print(f"[CONFIRM DATA] MISMATCH: {msg}")
                    return {
                        "status": "MISMATCH",
                        "pair_ok": False,
                        "mismatch_detected": True,
                        "mismatch_type": "mismatch",
                        "mismatch_message": msg,
                        "expected_fpc": expected_fpc_for_header,
                        "scanned_fpc": clean_fpc,
                        "expected_header": expected_header_for_fpc,
                        "scanned_header": clean_header,
                        "fpc_id": clean_fpc,
                        "header_id": clean_header,
                        "touchdown": None,
                        "pm_date": None,
                        "comment": None
                    }

            # =========================================================================
            # กรณีที่ 2: สแกนเฉพาะ Header (Header Only)
            # =========================================================================
            elif clean_header:
                if not row_by_header:
                    msg = f"Tag Header ({clean_header}) ไม่พบข้อมูลในระบบ Smart Store หรือยังไม่ได้ลงทะเบียนตู้ Store"
                    return {
                        "status": "NOT_FOUND",
                        "pair_ok": False,
                        "mismatch_detected": True,
                        "mismatch_type": "not_found",
                        "mismatch_message": msg,
                        "fpc_id": None,
                        "header_id": clean_header,
                        "touchdown": None,
                        "pm_date": None,
                        "comment": None
                    }
                else:
                    expected_f = (row_by_header.get('fpc_id') or '').strip()
                    td = row_by_header.get('touchdown')
                    pm = str(row_by_header.get('latest_pm')) if row_by_header.get('latest_pm') else None
                    comm = row_by_header.get('comment')
                    return {
                        "status": "HEADER_VERIFIED",
                        "pair_ok": None,
                        "mismatch_detected": False,
                        "mismatch_type": None,
                        "mismatch_message": None,
                        "fpc_id": None,
                        "header_id": clean_header,
                        "expected_fpc": expected_f,
                        "touchdown": None,
                        "pm_date": None,
                        "comment": None
                    }

            # =========================================================================
            # กรณีที่ 3: สแกนเฉพาะ FPC (FPC Only)
            # =========================================================================
            elif clean_fpc:
                if not row_by_fpc:
                    msg = f"Tag FPC ({clean_fpc}) ไม่พบข้อมูลในระบบ Smart Store หรือยังไม่ได้ทำ Data Mapping จากตู้ Store"
                    return {
                        "status": "NOT_FOUND",
                        "pair_ok": False,
                        "mismatch_detected": True,
                        "mismatch_type": "not_found",
                        "mismatch_message": msg,
                        "fpc_id": clean_fpc,
                        "header_id": None,
                        "touchdown": None,
                        "pm_date": None,
                        "comment": None
                    }
                else:
                    expected_h = (row_by_fpc.get('header_id') or '').strip()
                    td = row_by_fpc.get('touchdown')
                    pm = str(row_by_fpc.get('latest_pm')) if row_by_fpc.get('latest_pm') else None
                    comm = row_by_fpc.get('comment')
                    return {
                        "status": "FPC_VERIFIED",
                        "pair_ok": None,
                        "mismatch_detected": False,
                        "mismatch_type": None,
                        "mismatch_message": None,
                        "fpc_id": clean_fpc,
                        "expected_header": expected_h,
                        "header_id": None,
                        "touchdown": None,
                        "pm_date": None,
                        "comment": None
                    }

        except Exception as e:
            print(f"[CONFIRM DATA ERROR] {e}")
            return {
                "status": "ERROR",
                "pair_ok": False,
                "mismatch_detected": True,
                "mismatch_type": "error",
                "mismatch_message": f"เกิดข้อผิดพลาดในการตรวจสอบฐานข้อมูล: {e}",
                "fpc_id": clean_fpc,
                "header_id": clean_header or None,
                "touchdown": None,
                "pm_date": None,
                "comment": None
            }

    @staticmethod
    def get_probe_card_details(fpc_id: str) -> dict:
        """[CONFIRM DATA] ดึงรายละเอียด Probe Card จาก smart_store_probe_card"""
        res = DatabaseManager.confirm_probe_card_data(fpc_id)
        if res.get('status') in ['MATCH_OK', 'FPC_VERIFIED']:
            return {
                'touchdown': res.get('touchdown'),
                'pm_date': res.get('pm_date'),
                'comment': res.get('comment'),
                'header_id': res.get('expected_header') or res.get('header_id')
            }
        return None

    @staticmethod
    def is_active_pair(header_id: str, fpc_id: str) -> tuple[bool, bool]:
        """
        [CONFIRM DATA] ตรวจสอบ active_match และ allowed_pair ผ่าน smart_store_probe_card
        """
        res = DatabaseManager.confirm_probe_card_data(fpc_id, header_id)
        if res.get('status') == 'MATCH_OK':
            return True, True
        return False, False

    @staticmethod
    def get_enrichment_for_fpc(fpc_id: str) -> tuple[dict | None, dict | None]:
        """
        [CONFIRM DATA] คืนค่า (batch_lot, summary) สำหรับ FPC โดยดึงตรงจาก smart_store_probe_card
        """
        bl = None
        try:
            bl = DatabaseManager.get_batch_info_by_fpc(fpc_id)
        except Exception:
            pass

        summ = None
        try:
            res = DatabaseManager.confirm_probe_card_data(fpc_id)
            if res.get('status') in ['MATCH_OK', 'FPC_VERIFIED', 'MISMATCH']:
                summ = {
                    'touchdown': res.get('touchdown'),
                    'pm_date': res.get('pm_date'),
                    'comment': res.get('comment')
                }
        except Exception as e:
            print(f"[CONFIRM DATA] enrichment failed: {e}")
        return bl, summ

    @staticmethod
    def is_pair_allowed(fpc_id, header_id) -> bool:
        """[CONFIRM DATA] ตรวจสอบว่าคู่นี้อนุญาตหรือไม่"""
        res = DatabaseManager.confirm_probe_card_data(fpc_id, header_id)
        return res.get('status') == 'MATCH_OK'

    @staticmethod
    def is_header_active_for_fpc(header_id, fpc_id) -> bool:
        """[CONFIRM DATA] ตรวจสอบว่า Header กำลัง active กับ FPC นี้หรือไม่"""
        res = DatabaseManager.confirm_probe_card_data(fpc_id, header_id)
        return res.get('status') == 'MATCH_OK'

    @staticmethod
    def get_cassette_details(cassette_id):
        """
        [CONFIRM DATA: CASSETTE] ดึงข้อมูล Cassette จากตาราง smart_store_cabinet
        """
        if not cassette_id:
            return None
        clean_tag = str(cassette_id).strip()
        try:
            conn = DatabaseManager.get_store_connection()
            cur = conn.cursor(dictionary=True)
            cur.execute("""
                SELECT tag_id, lot_id, batch_id, mapping_time
                FROM smart_store_cabinet
                WHERE LOWER(REPLACE(tag_id, ' ', '')) = LOWER(REPLACE(%s, ' ', ''))
                LIMIT 1
            """, (clean_tag,))
            row = cur.fetchone()
            cur.close()
            conn.close()

            if row:
                m_time = str(row.get('mapping_time')) if row.get('mapping_time') else None
                return {
                    "cassette_id": row.get('tag_id') or clean_tag,
                    "machine_status": "Active",
                    "lot_id": row.get('lot_id'),
                    "batch_id": row.get('batch_id'),
                    "mapping_time": m_time,
                    "last_cleaning": None,
                    "next_cleaning": None
                }
            else:
                return {
                    "cassette_id": clean_tag,
                    "machine_status": "Unmapped",
                    "lot_id": f"UNMAPPED-{clean_tag[-6:]}",
                    "batch_id": "NOT_IN_STORE",
                    "mapping_time": None,
                    "last_cleaning": None,
                    "next_cleaning": None
                }
        except Exception as e:
            print(f"[CASSETTE STORE ERROR] {e}")
            return {
                "cassette_id": clean_tag,
                "machine_status": "Active",
                "lot_id": f"LOT-{clean_tag}",
                "batch_id": f"BATCH-{clean_tag}",
                "mapping_time": None,
                "last_cleaning": None,
                "next_cleaning": None
            }

    @staticmethod
    def store_cassette_log(cassette_id, machine_status, lot_id, batch_id, last_cleaning, next_cleaning, timestamp):
        """Insert a scan record into cassette_reader_log"""
        try:
            conn = DatabaseManager.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO cassette_reader_log 
                    (cassette_id, machine_status, lot_id, batch_id, last_cleaning, next_cleaning, machine_no, timestamp, synced)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0)
            """, (cassette_id, machine_status, lot_id, batch_id, last_cleaning, next_cleaning, Config.MACHINE_NO, timestamp))
            conn.commit()
            conn.close()
            print(f"[CASSETTE LOG STORED] {cassette_id} at {timestamp}")
            return True
        except Exception as e:
            print(f"[ERROR] DB insert (cassette_reader_log): {e}")
            return False



def push_unsynced_header():
    conn = DatabaseManager.get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT log_id, header_id, machine_no, timestamp
        FROM header_reader_log WHERE synced=0
        ORDER BY timestamp ASC LIMIT 200
    """)
    rows = cur.fetchall()
    if not rows:
        conn.close(); return

    payload = {"logs": [
        # NOTE: use TitleCase keys to match main server
        {"Header_id": r[1], "Machine_No": r[2], "Timestamp": (r[3].strftime('%Y-%m-%d %H:%M:%S') if hasattr(r[3], 'strftime') else str(r[3])) if r[3] else None}
        for r in rows
    ]}

    # 🔁 REPLACE your existing POST block with THIS:
    import requests
    try:
        r = requests.post(f"{Config.MAIN_SERVER_URL}/api/replicate_log",
                          json=payload, timeout=5)
        print("[SYNC] POST /api/replicate_log ->", r.status_code, r.text)
        ok = r.ok and r.json().get("inserted", 0) >= 0
    except Exception as e:
        print("[SYNC] push failed:", e)
        ok = False

    if ok:
        ids = [r[0] for r in rows]
        cur.execute(
            f"UPDATE header_reader_log SET synced=1 "
            f"WHERE log_id IN ({','.join(['%s']*len(ids))})", ids
        )
        conn.commit()

    conn.close()



def push_scan_logs_to_main():
    """Push unsynced scan_log entries to main server"""
    try:
        conn = DatabaseManager.get_connection()
        cur = conn.cursor()
        
        # Get unsynced scan logs
        cur.execute("""
            SELECT id, source, header_id, header_name, fpc_id,
                   batch_id, lot_id, touchdown, latest_pm, comment,
                   agv_no, machine_no, timestamp
            FROM scan_log
            WHERE synced = 0
            ORDER BY timestamp ASC
            LIMIT 100
        """)
        rows = cur.fetchall()
        
        if not rows:
            conn.close()
            return
            
        # Convert to the format expected by main server
        payload = {"logs": []}
        row_ids = []
        
        for r in rows:
            row_ids.append(r[0])  # Store the ID for marking as synced
            payload["logs"].append({
                "source": r[1],
                "header_id": r[2], 
                "header_name": r[3],
                "fpc_id": r[4],
                "batch_id": r[5],
                "lot_id": r[6], 
                "touchdown": r[7],
                "latest_pm": (r[8].strftime('%Y-%m-%d') if hasattr(r[8], 'strftime') else str(r[8])) if r[8] else None,
                "comment": r[9],
                "agv_no": r[10],
                "machine_no": r[11],
                "timestamp": (r[12].strftime('%Y-%m-%d %H:%M:%S') if hasattr(r[12], 'strftime') else str(r[12])) if r[12] else None
            })
        
        # Send to main server
        response = requests.post(
            f"{Config.MAIN_SERVER_URL}/api/receive_scan_logs", 
            json=payload, 
            timeout=10
        )
        
        if response.ok:
            result = response.json()
            if result.get('status') == 'success':
                # Mark as synced
                placeholders = ','.join(['%s'] * len(row_ids))
                cur.execute(f"""
                    UPDATE scan_log SET synced = 1 
                    WHERE id IN ({placeholders})
                """, row_ids)
                conn.commit()
                print(f"[PUSH] Successfully synced {len(row_ids)} scan logs to main server")
            else:
                print(f"[PUSH] Main server reported error: {result}")
        else:
            print(f"[PUSH] Failed to send scan logs: {response.status_code} {response.text}")
            
        conn.close()
            
    except Exception as e:
        print(f"[PUSH] Error pushing scan logs: {e}")

def sync_loop():
    while True:
        try:
            # Push our header logs to main server
            push_unsynced_header()
            
            # Push our scan logs to main server  
            push_scan_logs_to_main()
            
        except Exception as e:
            print(f"[SYNC] Error in sync loop: {e}")
        
        time.sleep(10)  # Sync every 10 seconds

# =============================================================================
# MIR ROBOT API
# =============================================================================

class MiRAPI:
    """Handles MiR robot API communications"""
    
    @staticmethod
    def get_plc_register(register_id=10):
        """Fetch PLC register value from MiR robot"""
        url = f"{Config.MIR_URL}/registers/{register_id}"
        try:
            response = requests.get(url, headers=Config.HEADERS, auth=Config.AUTH)
            if response.status_code == 200:
                data = response.json()
                return data.get("value", "Not available")
            else:
                print(f"Error {response.status_code}: {response.text}")
                return None
        except Exception as e:
            print(f"Exception occurred while getting register {register_id}: {e}")
            return None


# =============================================================================
# RFID READER CLASS
# =============================================================================
# ---------------- YRM100 minimal protocol helpers ----------------
YRM_START = 0xBB
YRM_END   = 0x7E
CMD_SINGLE = bytes([0xBB, 0x00, 0x22, 0x00, 0x00, 0x22, 0x7E])  # single inventory
CMD_GET_TX    = bytes([0xBB, 0x00, 0xB7, 0x00, 0x00, 0xB7, 0x7E])  # get tx power
CMD_GET_QUERY = bytes([0xBB, 0x00, 0x0D, 0x00, 0x00, 0x0D, 0x7E])  # get query

def read_frame(ser, timeout_s=0.25):
    """Return (ftype, cmd, payload) or None"""
    t_end = time.time() + timeout_s
    while time.time() < t_end:
        b = ser.read(1)
        if not b:
            return None
        if b[0] == 0xBB:
            break
    else:
        return None

    hdr = ser.read(4)
    if len(hdr) != 4: return None
    ftype, cmd, pl_msb, pl_lsb = hdr
    plen = (pl_msb << 8) | pl_lsb
    payload = ser.read(plen)
    if len(payload) != plen: return None
    ser.read(1)  # checksum skip
    end = ser.read(1)
    if len(end) != 1 or end[0] != 0x7E: return None
    return ftype, cmd, payload

# ---------------- Feature helpers --------------------------
def get_tx_power_dbm(ser):
    ser.write(CMD_GET_TX)
    fr = read_frame(ser, timeout_s=0.5)
    if fr and fr[0] == 0x01 and fr[1] == 0xB7 and len(fr[2]) == 2:
        raw = (fr[2][0] << 8) | fr[2][1]
        return raw / 100.0
    return None

def set_tx_power_dbm(ser, power_dbm):
    val = int(round(power_dbm * 100))
    payload = bytes([(val >> 8) & 0xFF, val & 0xFF])
    body = bytes([0x00, 0xB6, 0x00, 0x02]) + payload
    cs = sum(body) & 0xFF
    frame = bytes([0xBB]) + body + bytes([cs, 0x7E])
    ser.write(frame)
    fr = read_frame(ser, timeout_s=0.6)
    return bool(fr and fr[0] == 0x01 and fr[1] == 0xB6 and fr[2] == b"\x00")

def get_query_params(ser):
    ser.write(CMD_GET_QUERY)
    fr = read_frame(ser, timeout_s=0.6)
    if fr and fr[0] == 0x01 and fr[1] == 0x0D and len(fr[2]) == 2:
        return fr[2][0], fr[2][1]
    return None

def decode_query(msb, lsb):
    dr     = (msb >> 7) & 1
    m      = (msb >> 5) & 0b11
    trext  = (msb >> 4) & 1
    sel    = (msb >> 2) & 0b11
    sess   = msb & 0b11
    target = (lsb >> 7) & 1
    q      = (lsb >> 3) & 0x0F

    # Map to human-readable LinkMode
    dr_str = "8" if dr == 1 else "64/3"
    m_map  = {0b00: 1, 0b01: 2, 0b10: 4, 0b11: 8}
    m_str  = m_map.get(m, "?")
    link_mode = f"DR={dr_str}, M={m_str}, TRext={trext}"

    return {
        "DR": dr,
        "M": m_str,
        "TRext": trext,
        "Sel": sel,
        "Session": sess,
        "Target": "B" if target else "A",
        "Q": q,
        "LinkMode": link_mode
    }

def yrm_read_frame(ser, timeout_s=0.25):
    """Return (type, cmd, payload) or None."""
    t_end = time.time() + timeout_s
    while time.time() < t_end:
        b = ser.read(1)
        if not b:
            return None
        if b[0] == YRM_START:
            break
    else:
        return None

    hdr = ser.read(4)
    if len(hdr) != 4: return None
    ftype, cmd, pl_msb, pl_lsb = hdr
    plen = (pl_msb << 8) | pl_lsb
    payload = ser.read(plen)
    if len(payload) != plen: return None
    ser.read(1)  # checksum ignored (keep it short)
    end = ser.read(1)
    if len(end) != 1 or end[0] != YRM_END: return None
    return ftype, cmd, payload

read_frame = yrm_read_frame

def set_q(ser, q_val):
    if not (0 <= q_val <= 15):
        raise ValueError("Q must be 0-15")
    cur = get_query_params(ser)
    if not cur: return False
    msb, lsb = cur
    lsb = (lsb & 0b10000111) | ((q_val & 0x0F) << 3)
    payload = bytes([msb, lsb])
    body = bytes([0x00, 0x0E, 0x00, 0x02]) + payload
    cs = sum(body) & 0xFF
    frame = bytes([0xBB]) + body + bytes([cs, 0x7E])
    ser.write(frame)
    fr = yrm_read_frame(ser, timeout_s=0.6)
    return bool(fr and fr[0] == 0x01 and fr[1] == 0x0E and fr[2] == b"\x00")

def try_read_epc(ser, attempts=3):
    for _ in range(attempts):
        try:
            ser.write(CMD_SINGLE)
        except Exception:
            return None
        t_end = time.time() + 0.15
        while time.time() < t_end:
            fr = yrm_read_frame(ser, timeout_s=0.05)
            if not fr: continue
            ftype, cmd, payload = fr
            if ftype == 0x02 and cmd == 0x22 and len(payload) >= 5:
                epc_len = len(payload) - 5
                if epc_len > 0:
                    return payload[3:3+epc_len].hex().upper()
            elif ftype == 0x01 and cmd == 0xFF and payload == b"\x15":
                break
    return None

def yrm_single_inventory_once(ser, collect_window_s=0.15):
    """
    Send one single-inventory and return the first EPC (hex string) we see,
    or None if no tag.
    NOTICE frame layout: RSSI(1) | PC(2) | EPC(variable) | CRC(2)
    """
    import time
    ser.write(CMD_SINGLE)
    t_end = time.time() + collect_window_s
    while time.time() < t_end:
        fr = yrm_read_frame(ser, timeout_s=0.05)
        if not fr:
            continue
        ftype, cmd, p = fr
        # tag notice
        if ftype == 0x02 and cmd == 0x22 and len(p) >= 1+2+2:
            epc_len = len(p) - (1 + 2 + 2)
            if epc_len > 0:
                epc_hex = p[3:3+epc_len].hex().upper()
                return epc_hex
        # no-tag response
        elif ftype == 0x01 and cmd == 0xFF and p == b"\x15":
            return None
    return None

# -----------------------------------------------------------------
# Cassette Reader helper: supports USB HID / SmartCard & COM ports
# -----------------------------------------------------------------
def is_cassette_hw_connected() -> bool:
    """
    Check if Cassette Reader (OMNIKEY 5127 CK or Serial COM Reader) is physically connected.
    Supports:
    1. USB SmartCard / HID composite mode (VID_076B & PID_5128 / PID_5127) on Windows & Linux
    2. Virtual COM port mode (via serial.tools.list_ports)
    """
    # 1. Check COM ports first
    try:
        cass_port = str(getattr(Config, 'RFID_PORT_CASSETTE', '') or '').upper()
        for p in list_ports.comports():
            hwid = (p.hwid or "").upper()
            desc = (p.description or "").upper()
            device = (p.device or "").upper()
            if "076B" in hwid or "5128" in hwid or "5127" in desc or "OMNIKEY" in desc or (cass_port and device == cass_port):
                return True
    except Exception:
        pass

    # 2. Check Windows PnP Active Device Tree (CfgMgr32 API - instant native check)
    if platform.system() == "Windows":
        try:
            import ctypes
            cfgmgr32 = ctypes.windll.cfgmgr32
            CM_GETIDLIST_FILTER_PRESENT = 0x100
            buf_len = ctypes.c_ulong(0)
            if cfgmgr32.CM_Get_Device_ID_List_SizeW(ctypes.byref(buf_len), None, CM_GETIDLIST_FILTER_PRESENT) == 0 and buf_len.value > 0:
                buf = ctypes.create_unicode_buffer(buf_len.value)
                if cfgmgr32.CM_Get_Device_ID_ListW(None, buf, buf_len.value, CM_GETIDLIST_FILTER_PRESENT) == 0:
                    raw_str = "".join(buf)
                    for part in raw_str.split('\x00'):
                        u = part.upper()
                        if "VID_076B" in u or "PID_5128" in u or "PID_5127" in u or "OMNIKEY" in u:
                            return True
        except Exception:
            pass

    # 3. Check Linux USB sysfs
    elif platform.system() == "Linux":
        try:
            import glob
            for f in glob.glob("/sys/bus/usb/devices/*/idVendor"):
                with open(f, 'r') as fp:
                    if fp.read().strip().lower() == "076b":
                        return True
        except Exception:
            pass

    return False

# -----------------------------------------------------------------
# Sensor helper: supports GPIO (Pi) or MiR register polling
# -----------------------------------------------------------------
def _safe_import_gpio():
    try:
        import RPi.GPIO as GPIO
        return GPIO
    except Exception:
        return None

class SensorGate:
    """
    ============================================================================
    คลาส SensorGate (ระบบจัดการและควบคุมสัญญาณเซนเซอร์ FPC):
    ============================================================================
    ทำหน้าที่ตรวจสอบสถานะการเสียบ/ถอดแผ่นการ์ด FPC:
    1. โหมดหน้างานจริง (Production):
       - ตรวจจับสัญญาณไฟฟ้าจากขา GPIO Pin 6 (หรือ MiR PLC) เมื่อมีแผ่นการ์ดบังเซนเซอร์
    2. โหมดจำลองบนคอมพิวเตอร์ (Simulator Mode):
       - เปิดใช้งานเมื่อรันบน Windows / Notebook ที่ไม่มี GPIO
       - สามารถคลิกปุ่มป้าย Sensor บนหน้าเว็บ หรือกดปุ่ม 't' บนคีย์บอร์ดเพื่อสั่งสลับสถานะ ON/OFF
    ============================================================================
    """
    def __init__(self):
        self.mode = getattr(Config, 'SENSOR_MODE', 'GPIO').upper()
        self.active_high = bool(getattr(Config, 'SENSOR_ACTIVE_HIGH', True))
        self.GPIO = None
        self._setup_done = False
        # --- สถานะการจำลองเซนเซอร์ (สำหรับทดสอบ) ---
        self._simulate = bool(getattr(Config, 'SIMULATE_SENSOR_WITH_KEYBOARD', False))
        self._sim_state = False  # เริ่มต้นเป็น INACTIVE (OFF)
        self._sim_key = str(getattr(Config, 'SENSOR_TOGGLE_KEY', 't')).lower()


        if self.mode == 'GPIO':
            self.GPIO = _safe_import_gpio()
            if self.GPIO is not None:
                try:
                    self.GPIO.setmode(self.GPIO.BCM)
                    self.GPIO.setup(Config.SENSOR_PIN, self.GPIO.IN, pull_up_down=self.GPIO.PUD_DOWN if self.active_high else self.GPIO.PUD_UP)
                    self._setup_done = True
                except Exception as e:
                    print("[SENSOR] GPIO setup failed:", e)
            else:
                print("[SENSOR] RPi.GPIO not available; GPIO mode will always be inactive.")
        elif self.mode == 'MIR':
            self._setup_done = True  # nothing to init
        else:
            print(f"[SENSOR] Unknown SENSOR_MODE={self.mode}")
        
        # start keyboard listener if simulation enabled
        if self._simulate:
            try:
                threading.Thread(target=self._kb_loop, daemon=True).start()
                print(f"[SENSOR] Keyboard simulation ON - press '{self._sim_key.upper()}' to toggle ACTIVE/INACTIVE")
            except Exception as e:
                print("[SENSOR] keyboard listener failed:", e)


    def _kb_loop(self):
        """
        Cross-platform key listener.
        Windows: uses msvcrt
        POSIX  : uses select + tty/termios
        Press Config.SENSOR_TOGGLE_KEY to toggle the simulated sensor.
        """
        try:
            # --- Windows path ---
            import msvcrt  # type: ignore
            while True:
                if msvcrt.kbhit():
                    ch = msvcrt.getch()
                    if not ch:
                        continue
                    # decode to str if possible
                    try:
                        key = ch.decode('utf-8', errors='ignore').lower()
                    except Exception:
                        key = ''
                    if key == self._sim_key:
                        self._sim_state = not self._sim_state
                        print(f"[SENSOR] keyboard toggle -> {'ACTIVE' if self._sim_state else 'INACTIVE'}")
                time.sleep(0.05)
        except ImportError:
            # --- POSIX path ---
            import sys, select, termios, tty
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                while True:
                    rlist, _, _ = select.select([sys.stdin], [], [], 0.05)
                    if rlist:
                        ch = sys.stdin.read(1).lower()
                        if ch == self._sim_key:
                            self._sim_state = not self._sim_state
                            print(f"[SENSOR] keyboard toggle -> {'ACTIVE' if self._sim_state else 'INACTIVE'}")
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def is_active(self) -> bool:
        # Simulator / API toggle takes precedence when active or when GPIO is not available
        if getattr(self, '_simulate', False) or (self.mode == 'GPIO' and self.GPIO is None) or getattr(self, '_sim_state', False):
            return bool(self._sim_state)

        # Real sensor paths (GPIO or MiR)
        try:
            if not self._setup_done:
                return bool(self._sim_state)
            if self.mode == 'GPIO':
                if self.GPIO is None:
                    return bool(self._sim_state)
                val = self.GPIO.input(Config.SENSOR_PIN)
                return (val == 1) if self.active_high else (val == 0)
            elif self.mode == 'MIR':
                v = MiRAPI.get_plc_register(getattr(Config, 'SENSOR_MIR_REGISTER', 82))
                try:
                    iv = int(v)
                except Exception:
                    return bool(self._sim_state)
                return (iv == 1) if self.active_high else (iv == 0)
            return bool(self._sim_state)
        except Exception as e:
            print("[SENSOR] read error:", e)
            return bool(self._sim_state)

    # def is_active(self) -> bool:
    #     try:
    #         if not self._setup_done:
    #             return False
    #         if self.mode == 'GPIO':
    #             if self.GPIO is None:
    #                 return False
    #             val = self.GPIO.input(Config.SENSOR_PIN)
    #             return (val == 1) if self.active_high else (val == 0)
    #         elif self.mode == 'MIR':
    #             v = MiRAPI.get_plc_register(getattr(Config, 'SENSOR_MIR_REGISTER', 82))
    #             if v is None:
    #                 return False
    #             return (int(v) == 1) if self.active_high else (int(v) == 0)
    #         return False
    #     except Exception as e:
    #         print("[SENSOR] read error:", e)
    #         return False
        


class RFIDReader:
    def __init__(self, port=Config.RFID_PORT, baudrate=Config.RFID_BAUDRATE, reader_mode=None):
        self.port = port
        self.baudrate = baudrate
        self.reader_mode = reader_mode  # if None, defaults to Config.READER_MODE
        self.ser = None
        self.running = False
        self.last_tag = None
        self.last_seen = 0
        self.header_logged_id = None      # one-shot latch for header log
        self.header_logged_ts = None
        self.thread = None
        self.current_data = {
            "fpc_id": None,
            "header_id": None,
            "header_name": None,
            "batch_id": None,
            "lot_id": None,
            "touchdown": None,
            "pm_date": None,
            "comment": None,
            "agv_no": Config.AGV_NO,
            "machine_no": Config.MACHINE_NO,
            "timestamp": None,
            "cassette_id": None
        }

    def connect(self):
        """Connect only to the configured COM port, no auto-switch."""
        try:
            if self.ser is None or not self.ser.is_open:
                self.ser = serial.Serial(self.port, self.baudrate, timeout=1, write_timeout=0.5)
                print(f"[CONNECTED] to {self.port}")
            return True
        except Exception as e:
            print(f"[ERROR] Failed to open {self.port}: {e}")
            return False
        
    def close(self):
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
        except Exception: pass
        self.ser = None

    def is_hw_connected(self):
        """Return True if the configured COM port or USB device is present."""
        if getattr(self, 'reader_mode', None) == 'CASSETTE':
            return is_cassette_hw_connected()
        try:
            ports = [p.device.upper() for p in list_ports.comports()]
            present = (self.port or "").upper() in ports

            if not present:
                try:
                    if self.ser and self.ser.is_open:
                        self.ser.close()
                except Exception:
                    pass
                self.ser = None
                self.running = False
            return present
        except Exception:
            return False
        
    # --- feature wrappers
    def get_tx_power_dbm(self): return get_tx_power_dbm(self.ser) if self.connect() else None
    def set_tx_power_dbm(self, dbm): return set_tx_power_dbm(self.ser, dbm) if self.connect() else False
    def get_query_params(self): return get_query_params(self.ser) if self.connect() else None
    def decode_query(self, msb, lsb): return decode_query(msb, lsb)
    def set_q(self, q): return set_q(self.ser, q) if self.connect() else False

    def single_epc_ascii(self, attempts=3):
        epc_hex = try_read_epc(self.ser, attempts) if self.connect() else None
        if not epc_hex: return None
        try: raw = bytes.fromhex(epc_hex)
        except ValueError: return None
        raw = raw.split(b"\x00", 1)[0]
        s = raw.decode("ascii", errors="ignore")
        return "".join(c for c in s if 32 <= ord(c) <= 126).strip()
        

    

    def get_current_data(self):
        """Get current RFID data"""
        return self.current_data



    def start_reading(self):
        if self.running: 
            return True
        if not self.connect(): 
            return False
        self.running = True
    # --- Diagnostics on startup ---
        try:
            power = self.get_tx_power_dbm()
            q_val = None
            qp = self.get_query_params()
            if qp:
                msb, lsb = qp
                q_val = (lsb >> 3) & 0x0F
                decoded = self.decode_query(msb, lsb)
            else:
                decoded = None

            print("========== YRM100 STARTUP DIAGNOSTICS ==========")
            print(f"TX Power (dBm): {power}")
            print(f"Q Value: {q_val}")
            print("Query now:",
                  f"DR={decoded['DR']}, M={decoded['M']}, TRext={decoded['TRext']}, "
                  f"Sel={decoded['Sel']}, Session={decoded['Session']}, "
                  f"Target={decoded['Target']}, Q={decoded['Q']} "
                  f"({decoded['LinkMode']})")
            print("================================================")
        except Exception as e:
            print(f"[WARN] Could not fetch startup diagnostics: {e}")
        self.thread = threading.Thread(target=self._read_loop, daemon=True)
        self.thread.start()
        return True

    def _read_loop(self):
        print("[LISTENING] YRM100...")
        while self.running:
            try:
                now = time.time()
                epc_ascii = self.single_epc_ascii()
                if epc_ascii:
                    mode = self.reader_mode if self.reader_mode else getattr(Config, "READER_MODE", "HEADER").upper()
                    # [RF CROSSTALK FILTER] Prevent Header reader from picking up FPC tags and vice versa
                    if mode == "HEADER" and DatabaseManager.is_known_fpc_tag(epc_ascii):
                        # FPC tag detected in Header reader RF range: IGNORE it completely
                        time.sleep(getattr(Config, "YRM100_GAP_S", 1.0))
                        continue
                    elif mode == "FPC" and DatabaseManager.is_known_header_tag(epc_ascii):
                        # Header tag detected in FPC reader RF range: IGNORE it completely
                        time.sleep(getattr(Config, "YRM100_GAP_S", 1.0))
                        continue

                    if epc_ascii != self.last_tag or self.last_tag is None:
                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        if mode == "HEADER":
                            self.current_data.update({"header_id": epc_ascii, "fpc_id": None, "timestamp": timestamp})
                        elif mode == "CASSETTE":
                            self.current_data.update({"cassette_id": epc_ascii, "timestamp": timestamp})
                        else:
                            self.current_data.update({"fpc_id": epc_ascii, "header_id": None, "timestamp": timestamp})
                        self._process_new_tag(epc_ascii, timestamp)
                        print(f"[NEW TAG] {epc_ascii} at {timestamp}")
                    self.last_tag = epc_ascii
                    self.last_seen = now
                if self.last_tag and (now - self.last_seen > Config.TAG_TIMEOUT):
                    print(f"[TAG CLEARED] {self.last_tag}")
                    self._clear_current_data()
                time.sleep(getattr(Config, "YRM100_GAP_S", 1.0))
            except Exception as e:
                print("[ERROR] loop:", e)
                self.close()
                time.sleep(0.5)



    def _process_new_tag(self, tag_ascii: str, timestamp: str) -> None:
        """
        Handle a new RFID tag string.

        HEADER mode (reader #1, header-only):
        - Treat tag_ascii as header_id
        - DO NOT resolve or populate any FPC-related data (fpc_id, batch/lot, TD/PM/comment)
        - Store exactly once per "appearance" in header_reader_log (one-shot latch)
        - Do NOT write to scan_log here (we'll write to scan_log only when both readers match)

        FPC mode (reserved for reader #2 to be added later):
        - Treat tag_ascii as fpc_id
        - Optionally resolve header and summaries if desired
        - This block can still snapshot if you want, but for Phase 1 we keep behavior intact
        """
        try:
            mode = self.reader_mode if self.reader_mode else getattr(Config, 'READER_MODE', 'HEADER').upper().strip()

            if mode == 'CASSETTE':
                cassette_id = (tag_ascii or '').strip()
                self.current_data.update({
                    "cassette_id": cassette_id,
                    "timestamp": timestamp
                })
                return

            # Always start with a clean "current" skeleton; reader #1 will stay header-only.
            current = {
                'timestamp': timestamp,
                'machine_no': getattr(Config, 'MACHINE_NO', '-'),
                'agv_no': getattr(Config, 'AGV_NO', None),

                # What we will expose on /api/current_data
                'header_name': None,
                'header_id': None,
                'fpc_id': None,
                'batch_id': None,
                'lot_id': None,
                'touchdown': None,
                'pm_date': None,
                'comment': None,
            }

            # =========================
            # HEADER-ONLY READER (R#1)
            # =========================
            if mode == 'HEADER':
                header_id = (tag_ascii or '').strip()

                # live snapshot shows header only
                current['header_id'] = header_id
                current['header_name'] = None
                # explicit: make sure FPC-related fields are clean
                current['fpc_id']    = None
                current['batch_id']  = None
                current['lot_id']    = None
                current['touchdown'] = None
                current['pm_date']   = None
                current['comment']   = None

                # one-shot store to header_reader_log (no repeat until cleared)
                # NOTE: you must add self.header_logged_id and self.header_logged_ts
                # in __init__ (see notes below).
                if getattr(self, 'header_logged_id', None) != header_id:
                    DatabaseManager.store_header_log(header_id, timestamp)
                    self.header_logged_id = header_id
                    self.header_logged_ts = timestamp
                    print(f"[HEADER ONLY] stored once: header_id={header_id} @ {timestamp}")
                else:
                    print(f"[HEADER ONLY] already stored for {header_id}; skipping re-log")

                # IMPORTANT: we DO NOT write to scan_log here for HEADER mode.
                # Combined scan_log will be handled later when both readers are present.

                # merge to shared snapshot and exit early so we never fall through
                self.current_data.update(current)
                return

            # ======================================
            # (Reserved) FPC READER (R#2) - optional
            # ======================================
            elif mode == 'FPC':
                fpc_id = (tag_ascii or '').strip()
                current['fpc_id'] = fpc_id

                # If you want to keep the existing behavior for FPC mode,
                # resolve newest header and FPC summary. This mirrors your original code.
                try:
                    header_id = DatabaseManager.get_header_id(fpc_id)
                except Exception as _:
                    header_id = None
                current['header_id'] = header_id

                # store a per-read log (your original helper writes to fpc_reader_log)
                try:
                    DatabaseManager.store_fpc_log(fpc_id, timestamp)
                except Exception as _:
                    pass

                # FPC summary (touchdown/pm/comment)
                try:
                    summary = DatabaseManager.get_fpc_summary(fpc_id)
                    if summary:
                        current['touchdown'] = summary.get('touchdown')
                        current['pm_date']   = summary.get('pm_date')
                        current['comment']   = summary.get('comment')
                except Exception as _:
                    pass

                # batch/lot enrichment
                try:
                    bl = DatabaseManager.get_batch_info_by_fpc(fpc_id)
                    if bl:
                        current['batch_id'] = bl.get('batch_id')
                        current['lot_id']   = bl.get('lot_id')
                except Exception as _:
                    pass

                # For now, we keep your snapshot-to-scan_log behavior for non-HEADER modes.
                try:
                    src = 'FPC'
                    latest_pm_date = current.get('pm_date')

                    conn = DatabaseManager.get_connection()
                    cur = conn.cursor()
                    cur.execute("""
                        INSERT INTO scan_log
                            (source, header_id, header_name, fpc_id,
                            batch_id, lot_id, touchdown, latest_pm, comment,
                            agv_no, machine_no, timestamp)
                        VALUES
                            (%s, %s, %s, %s,
                            %s, %s, %s, %s, %s,
                            %s, %s, %s)
                    """, (
                        src,
                        current.get('header_id'),
                        current.get('header_name'),
                        current.get('fpc_id'),
                        current.get('batch_id'),
                        current.get('lot_id'),
                        current.get('touchdown'),
                        latest_pm_date,          # DATE string 'YYYY-MM-DD' or None
                        current.get('comment'),
                        current.get('agv_no'),
                        current.get('machine_no'),
                        current.get('timestamp'),
                    ))
                    conn.commit()
                    conn.close()
                except Exception as e:
                    print(f"[WARN] snapshot insert failed: {e}")

                # merge to shared snapshot
                self.current_data.update(current)

            else:
                print(f"[WARN] Unknown READER_MODE='{mode}'. No action taken.")

        except Exception as e:
            print(f"[ERROR] _process_new_tag: {e}")

    def _clear_current_data(self):
        """Clear current RFID data"""
        mode = self.reader_mode if self.reader_mode else getattr(Config, 'READER_MODE', 'HEADER').upper().strip()
        if mode == 'CASSETTE':
            self.current_data.update({
                'cassette_id': None,
                'timestamp': None
            })
        else:
            self.current_data.update({
                'fpc_id': None,
                'header_id': None,
                'header_name': None,  
                'timestamp': None,
                'touchdown': None,
                'pm_date': None,
                'comment': None,
                'batch_id': None,      
                'lot_id': None       
            })
        self.last_tag = None
        self.last_seen = 0
        self.header_logged_id = None
        self.header_logged_ts = None

class FPCReader:
    def __init__(self, port=Config.RFID_PORT_FPC, baudrate=Config.RFID_BAUDRATE):
        self.port = port
        self.baudrate = baudrate
        self.ser = None
        self.running = False
        self.thread = None

        self.sensor = SensorGate()
        self.window_open = False
        self.window_until = 0.0
        self.fpc_last = None
        self.fpc_current = None
        self.fpc_logged_latch = None   # one-shot per window
        self.window_just_closed = False     # set True when sensor goes LOW while a tag was held
        self.last_window_fpc_id = None      # the FPC that was held in the last window
        self.last_window_timestamp = None
        self.window_committed = False   # one-and-done per window
        self.block_until_low = False

        self.current_data = {
            "fpc_id": None,
            "timestamp": None,
        }

    def connect(self):
        try:
            if self.ser is None or not self.ser.is_open:
                if hasattr(Config, 'RFID_PORT_FPC') and Config.RFID_PORT_FPC:
                    self.port = Config.RFID_PORT_FPC
                self.ser = serial.Serial(self.port, self.baudrate, timeout=1, write_timeout=0.5)
                print(f"[FPC] connected {self.port}")
                # Set maximum transmit power (TX Power) for longest read range
                try:
                    target_pwr = float(getattr(Config, 'RFID_TX_POWER_FPC', 26.0))
                    set_tx_power_dbm(self.ser, target_pwr)
                    cur_pwr = get_tx_power_dbm(self.ser)
                    print(f"[FPC] TX Power set to {cur_pwr} dBm (target: {target_pwr} dBm)")
                    get_query_params(self.ser)
                except Exception as e:
                    print(f"[FPC] TX power init warning: {e}")
            return True
        except Exception as e:
            print(f"[FPC] open error: {e}")
            return False

    def close(self):
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
        except Exception:
            pass
        self.ser = None

    def is_hw_connected(self):
        """Return True if the configured COM port is present."""
        try:
            if hasattr(Config, 'RFID_PORT_FPC') and Config.RFID_PORT_FPC:
                self.port = Config.RFID_PORT_FPC
            all_ports = list_ports.comports()
            ports = [p.device.upper() for p in all_ports]
            present = (self.port or "").upper() in ports

            if not present:
                try:
                    if self.ser and self.ser.is_open:
                        self.ser.close()
                except Exception:
                    pass
                self.ser = None
                self.running = False
            return present
        except Exception:
            return False

    def start(self):
        if self.running:
            return True
        if not self.connect():
            return False
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return True

    def stop(self):
        self.running = False

    def _read_once_ascii(self):
        epc_hex = try_read_epc(self.ser, attempts=2) if self.connect() else None
        if not epc_hex:
            return None
        try:
            raw = bytes.fromhex(epc_hex)
        except ValueError:
            return None
        raw = raw.split(b"\x00", 1)[0]
        s = raw.decode("ascii", errors="ignore")
        return "".join(c for c in s if 32 <= ord(c) <= 126).strip() or None

    def _clear(self, reason=""):
        had_tag = bool(self.fpc_current)
        last = self.fpc_current

        # If clearing due to sensor LOW while a tag was held, raise one-shot flag
        if ("sensor LOW" in reason) and had_tag and (not self.window_committed):
            self.window_just_closed = True
            self.last_window_fpc_id = last
            self.last_window_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.window_committed = True
            print(f"[FPC] window closed flag set (sensor LOW): fpc={last} @ {self.last_window_timestamp}")

        if self.fpc_current and getattr(Config, 'BOTH_VERBOSE', False):
            print(f"[FPC] CLEAR ({reason}) fpc_id={self.fpc_current}")

        # Reset live state
        self.fpc_current = None
        self.current_data.update({"fpc_id": None, "timestamp": None})
        self.fpc_logged_latch = None
        self.window_open = False
        self.window_until = 0.0

    def _loop(self):
        print("[FPC] Sensor-Gated loop starting...")
        gap = getattr(Config, "YRM100_GAP_S", 0.5)
        while self.running:
            try:
                active = self.sensor.is_active()
                now = time.time()

                # open window on rising edge
                if active and not self.window_open and not self.block_until_low:
                    self.window_open = True
                    self.window_until = now + float(getattr(Config, 'FPC_WINDOW_S', 10.0))
                    self.fpc_logged_latch = None
                    self.window_committed = False
                    print(f"[FPC] sensor ACTIVE -> open window {getattr(Config, 'FPC_WINDOW_S', 10.0)}s")

                # if window open, try to read
                if self.window_open:
                    if not active:
                        # sensor dropped -> clear immediately and signal commit
                        self._clear("sensor LOW during window")
                    else:
                        # still active; within window?
                        if now <= self.window_until:
                            epc_ascii = self._read_once_ascii()
                            if epc_ascii:
                                if epc_ascii != self.fpc_current:
                                    self.fpc_current = epc_ascii
                                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                    self.current_data.update({"fpc_id": epc_ascii, "timestamp": ts})
                                    print(f"[FPC] READ fpc_id={epc_ascii} @ {ts}")
                                    if self.fpc_logged_latch != epc_ascii:
                                        DatabaseManager.store_fpc_log(epc_ascii, ts)
                                        self.fpc_logged_latch = epc_ascii
                        else:
                            # window expired:
                            if not self.fpc_current:
                                self._clear("window timeout (no read)")
                                self.block_until_low = True
                            else:
                                if not self.window_committed and not self.window_just_closed:
                                    self.window_just_closed = True
                                    self.last_window_fpc_id = self.fpc_current
                                    self.last_window_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                    self.window_committed = True
                else:
                    if not active:
                        if self.block_until_low:
                            self.block_until_low = False
                        if self.fpc_current:
                            self._clear("sensor LOW (idle)")

                time.sleep(gap)
            except Exception as e:
                print("[FPC] loop error:", e)
                self.close()
                time.sleep(0.5)

    def snapshot(self):
        return self.current_data.copy()

# =============================================================================
# BACKUP UTILITIES
# =============================================================================

class BackupManager:
    """Handles log backup operations"""
    
    @staticmethod
    def backup_logs_to_csv():
        """Backup today's logs to CSV with selected columns."""
        try:
            os.makedirs('logs', exist_ok=True)

            conn = DatabaseManager.get_connection()
            cursor = conn.cursor()
            today_str = datetime.now().strftime('%Y-%m-%d')

            query = """
                SELECT id, batch_id, lot_id, fpc_id, header_id, header_name,
                    timestamp, agv_no, machine_no
                FROM scan_log
                WHERE DATE(timestamp) = %s
                ORDER BY timestamp ASC
            """
            cursor.execute(query, (today_str,))
            rows = cursor.fetchall()
        except Exception as e:
            print(f"[BACKUP ERROR] {e}")
            try:
                conn.close()
            except Exception:
                pass
            return
        finally:
            try:
                conn.close()
            except Exception:
                pass

        if not rows:
            print("[BACKUP] No logs to export.")
            return

        filename = f'logs/{today_str}.csv'
        headers = [
            'id','batch_id','lot_id','fpc_id','header_id','header_name',
            'timestamp','agv_no','machine_no'
        ]

        # Write CSV (Excel-friendly)
        with open(filename, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for r in rows:
                r = list(r)
                # r[6] is the timestamp column from SELECT -> format as text
                if hasattr(r[6], 'strftime'):
                    r[6] = r[6].strftime('%Y-%m-%d %H:%M:%S')
                writer.writerow(r)

        print(f"[BACKUP] Logs saved to {filename}")

    @staticmethod
    def schedule_daily_backup():
        """Schedule next daily backup"""
        now = datetime.now()
        next_run = now.replace(hour=23, minute=59, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)

        delay = (next_run - now).total_seconds()
        print(f"[SCHEDULER] Next backup scheduled in {int(delay)} seconds ({next_run.strftime('%Y-%m-%d %H:%M:%S')})")
        threading.Timer(delay, BackupManager._run_backup_and_reschedule).start()

    @staticmethod
    def _run_backup_and_reschedule():
        """Run backup and schedule the next one"""
        BackupManager.backup_logs_to_csv()
        BackupManager.schedule_daily_backup()


# =============================================================================
# FLASK APPLICATION
# =============================================================================

class RFIDApp:
    """Main Flask application class"""
    
    def __init__(self):
        self.app = Flask(__name__)
        CORS(self.app, resources={r"/api/*": {"origins": "*"}})
        self.start_time = datetime.now()
        self.app.secret_key = os.environ.get("APP_SECRET", "dev-secret-change-me")
        CORS(self.app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)
        self.rfid_reader = None
        self.header_reader = None   # reader #1 (HEADER)
        self.fpc_reader = None      # reader #2 (FPC)
        self.cassette_reader = None # reader #3 (CASSETTE)
        self.last_pair_logged = None  # (header_id, fpc_id, ts)
        self._hdr_seen_in_window = None
        self._pair_state = {
            "pair_ok": None,            
            "pair_status": None,       
            "header_id": None,
            "fpc_id": None,
            "batch_id": None,
            "lot_id": None,
            "touchdown": None,
            "pm_date": None,
            "comment": None,
            "ts": None,                 
        }
        self._cassette_state = {
            "cassette_id": None,
            "machine_status": None,
            "lot_id": None,
            "batch_id": None,
            "last_cleaning": None,
            "next_cleaning": None,
            "timestamp": None,
            "stage": "IDLE"
        }
        self._cassette_stage = "IDLE"          # IDLE -> LOADED -> IN_PROCESS -> STANDBY -> IDLE
        self._cassette_active_tag = None
        self._cassette_last_seen = 0

        # Start cassette state machine timer loop
        threading.Thread(target=self._cassette_timer_loop, daemon=True).start()

        # Start both_logger_loop for FPC SensorGate + Header logging
        threading.Thread(target=self._both_logger_loop, daemon=True).start()

        # --- [NEW] Mockup Mode variables initialization ---
        # These variables store the simulated states for FPC and Cassette during mockup demo
        self._live_mock_fpc = {}
        self._live_mock_cassette = {}
        if getattr(Config, 'MOCKUP_MODE', False):
            print("[MOCK] Mockup Mode is enabled. Starting mockup simulation loop...")
            # Start background thread to simulate RFID tag readings
            t = threading.Thread(target=self._mockup_simulator_loop, daemon=True)
            t.start()

        # --- [PMI SIMULATION & IMAGE SERVICE STATE] ---
        self._pmi_sim_index = 0
        self._pmi_failed_records = []
        self._pmi_last_update = 0

        self._setup_routes()

    def admin_required(self, fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not session.get('employee_id'):
                return jsonify({"ok": False, "message": "not logged in"}), 401
            if session.get('role') != 'admin':
                return jsonify({"ok": False, "message": "forbidden"}), 403
            return fn(*args, **kwargs)
        return wrapper

    def login_required(self, fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not session.get('employee_id'):
                return jsonify({"ok": False, "message": "not logged in"}), 401
            return fn(*args, **kwargs)
        return wrapper

    def _setup_routes(self):
        """Setup Flask routes"""
        
        # Static file routes
        @self.app.route('/')
        def index():
            return send_from_directory('.', 'index.html')

        @self.app.route('/styles.css')
        def styles():
            return send_from_directory('.', 'styles.css')

        @self.app.route('/script.js')
        def script():
            return send_from_directory('.', 'script.js')

        @self.app.route('/NXP_logo.png')
        def logo():
            return send_from_directory('.', 'NXP_logo.png')

        # API routes
        @self.app.route('/api/current_data')
        def get_current_data():
            return self._get_current_data()

        # =====================================================================
        # --- [PMI SIMULATION & IMAGE SERVICE (UIIU Integration)] ---
        # =====================================================================
        def _get_pmi_dirs():
            base_dir = os.path.dirname(os.path.abspath(__file__))
            candidates = [
                os.path.join(base_dir, 'UIIU', 'simulation'),
                os.path.join(base_dir, 'UIIU', 'datasets'),
                os.path.join(base_dir, 'UIIU'),
                os.path.join(base_dir, '..', 'UIIU', 'simulation'),
                os.path.join(base_dir, '..', 'UIIU', 'datasets'),
                os.path.join(base_dir, '..', 'UIIU'),
                os.path.join(base_dir, '..', 'PUNPUNJA', 'PROJECT', 'UIIU', 'simulation'),
                os.path.join(base_dir, '..', 'PUNPUNJA', 'PROJECT', 'UIIU', 'datasets'),
                os.path.join(base_dir, '..', 'PUNPUNJA', 'PROJECT', 'UIIU'),
                '/home/nxp1/Desktop/PUNPUNJA/PROJECT/UIIU/simulation',
                '/home/nxp1/Desktop/PUNPUNJA/PROJECT/UIIU/datasets',
                '/home/nxp1/Desktop/PUNPUNJA/PROJECT/UIIU',
                '/home/root/UIIU/simulation',
                '/home/root/UIIU/datasets',
                '/home/root/UIIU',
                os.path.join(base_dir, 'simulation'),
            ]
            valid = []
            for c in candidates:
                if os.path.exists(c) and os.path.isdir(c):
                    valid.append(os.path.abspath(c))
            return valid

        def _get_pmi_images():
            dirs = _get_pmi_dirs()
            files = []
            exts = ('.png', '.jpg', '.jpeg', '.PNG', '.JPG', '.JPEG')
            for d in dirs:
                try:
                    for root, _, filenames in os.walk(d):
                        for f in filenames:
                            if f.endswith(exts):
                                files.append((root, f))
                except Exception:
                    pass
            return files

        @self.app.route('/api/latest-inspection')
        @self.app.route('/api/v1/latest-inspection')
        def pmi_latest_inspection():
            images = _get_pmi_images()
            now = time.time()
            if images:
                # Step to next image every 2.5 seconds
                if now - self._pmi_last_update > 2.5:
                    self._pmi_sim_index = (self._pmi_sim_index + 1) % len(images)
                    self._pmi_last_update = now

                img_dir, img_name = images[self._pmi_sim_index % len(images)]
                upper_name = img_name.upper()
                is_fail = ('FAIL' in upper_name or 'NG' in upper_name or 'DEFECT' in upper_name)
                decision = 'FAIL' if is_fail else 'PASS'

                record = {
                    "status": "success",
                    "image_name": img_name,
                    "filename": img_name,
                    "rawImageUrl": f"/api/pmi/image/{img_name}",
                    "annotatedImageUrl": f"/api/pmi/image/{img_name}",
                    "imageUrl": f"/api/pmi/image/{img_name}",
                    "decision": decision,
                    "ai_decision": decision,
                    "is_pass": (not is_fail),
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }

                if is_fail and not any(f.get('image_name') == img_name for f in self._pmi_failed_records):
                    self._pmi_failed_records.append(record)
                    if len(self._pmi_failed_records) > 20:
                        self._pmi_failed_records.pop(0)

                return jsonify(record)
            else:
                return jsonify({
                    "status": "success",
                    "image_name": "pmi_inspection.png",
                    "rawImageUrl": "/pmi_inspection.png",
                    "annotatedImageUrl": "/pmi_inspection.png",
                    "decision": "PASS",
                    "is_pass": True,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })

        @self.app.route('/api/batch-summary')
        @self.app.route('/api/v1/batch-summary')
        def pmi_batch_summary():
            return jsonify({
                "status": "success",
                "isBatchComplete": False,
                "failedRecords": self._pmi_failed_records
            })

        @self.app.route('/api/pmi/image/<path:img_filename>')
        def serve_pmi_image(img_filename):
            images = _get_pmi_images()
            for img_dir, name in images:
                if name == img_filename:
                    return send_from_directory(img_dir, name)
            return send_from_directory('.', img_filename)

        @self.app.route('/api/simulate/cassette')
        def simulate_cassette():
            tag = request.args.get('tag')
            clear = request.args.get('clear', 'false').lower() == 'true'
            connected = request.args.get('connected', 'true').lower() == 'true'

            self._cassette_simulated_connected = connected

            if clear:
                self._cassette_simulated = False
                for k in self._cassette_state:
                    self._cassette_state[k] = None
                return jsonify({"status": "success", "message": "Cassette simulation cleared"})
            
            if tag:
                self._cassette_simulated = True
                details = DatabaseManager.get_cassette_details(tag)
                if details:
                    self._cassette_state.update(details)
                    self._cassette_state['timestamp'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    # Log it
                    DatabaseManager.store_cassette_log(
                        tag,
                        details.get('machine_status'),
                        details.get('lot_id'),
                        details.get('batch_id'),
                        details.get('last_cleaning'),
                        details.get('next_cleaning'),
                        self._cassette_state['timestamp']
                    )
                    return jsonify({"status": "success", "message": f"Simulated scanning of cassette: {tag}", "data": self._cassette_state})
                return jsonify({"status": "error", "message": "Tag details could not be resolved"}), 400
            
            return jsonify({"status": "success", "message": "Cassette reader connection simulated", "connected": connected})

        # ============================================================================
        # CASSETTE RFID SCAN ENDPOINT (รับข้อมูลสแกนแท็ก Cassette จาก 5127 CK)
        # ============================================================================
        @self.app.route('/api/cassette/scan', methods=['GET', 'POST'])
        def api_cassette_scan():
            if request.method == 'POST':
                data = request.get_json(silent=True) or {}
                tag = data.get('cassette_id') or data.get('tag')
            else:
                tag = request.args.get('tag') or request.args.get('cassette_id')
            
            res = self._on_cassette_scan(tag)
            return jsonify(res)

        # ============================================================================
        # SENSOR SIMULATION & CONTROL ENDPOINTS (API ควบคุมเซนเซอร์จำลอง FPC)
        # ============================================================================
        # รองรับการคลิกปุ่ม Sensor Badge บนหน้าเว็บ GUI หรือสั่งงานผ่านคำสั่ง REST API:
        # - สลับสถานะเป็น 'ON'  (ACTIVE)  : หัวอ่าน FPC (COM6) จะเปิดรอบสแกน 8 วินาทีเพื่ออ่านแท็ก
        # - สลับสถานะเป็น 'OFF' (INACTIVE): เสมือนดึงแผ่น FPC ออก ข้อมูลในช่อง FPC จะถูกเคลียร์กลับเป็นค่าว่าง
        # ============================================================================
        @self.app.route('/api/toggle_sensor', methods=['GET', 'POST'])
        @self.app.route('/api/sensor/<action>', methods=['GET', 'POST'])
        def control_sensor(action="toggle"):
            """
            ควบคุมสถานะเซนเซอร์จำลอง:
            - action = 'toggle' : สลับสถานะ ON <-> OFF
            - action = 'on'     : สั่งให้ Sensor เป็น ON (เสียบการ์ด)
            - action = 'off'    : สั่งให้ Sensor เป็น OFF (ถอดการ์ด)
            """
            if getattr(self, 'fpc_reader', None) and getattr(self.fpc_reader, 'sensor', None):
                curr = getattr(self.fpc_reader.sensor, '_sim_state', False)
                if action == "on":
                    self.fpc_reader.sensor._sim_state = True
                elif action == "off":
                    self.fpc_reader.sensor._sim_state = False
                else:
                    self.fpc_reader.sensor._sim_state = not curr
                new_state = self.fpc_reader.sensor._sim_state
                print(f"[SENSOR API] Sensor simulation state -> {'ACTIVE (ON)' if new_state else 'INACTIVE (OFF)'}")
                return jsonify({"status": "success", "sensor_active": new_state})
            return jsonify({"status": "error", "message": "FPC reader or sensor not available"}), 400

        @self.app.route('/api/logs')
        def get_logs():
            return self._get_logs()

        @self.app.route('/api/search_logs')
        def search_logs():
            return self._search_logs()

        @self.app.route('/api/cassette/logs')
        def get_cassette_logs():
            return self._get_cassette_logs()

        @self.app.route('/api/cassette/search_logs')
        def search_cassette_logs():
            return self._search_cassette_logs()

        @self.app.route('/api/system_info')
        def get_system_info():
            return self._get_system_info()

        @self.app.route('/api/replicate_log', methods=['POST'])
        def replicate_log():
            return self._replicate_log()
        
        @self.app.route('/api/update_machine_name', methods=['POST'])
        def update_machine_name():
            """
            --- [NEW] API to update machine name ---
            Accepts JSON: {"machine_no": "AVT#XX"}
            Updates Config.MACHINE_NO in memory, and writes it back to config.py
            to persist the configuration across restarts.
            """
            try:
                # --- [NEW] Session check to only allow system admin ('ADMIN') ---
                logged_in_user = session.get('employee_id', '').strip().upper()
                if logged_in_user != 'ADMIN':
                    return jsonify({"status": "error", "message": "Unauthorized: Only system admin can rename the machine"}), 403

                data = request.json or {}
                new_name = (data.get('machine_no') or '').strip()
                if not new_name:
                    return jsonify({"status": "error", "message": "Machine name cannot be empty"}), 400

                # 1. Update in memory
                Config.MACHINE_NO = new_name

                # 2. Write to config.py to persist
                import os
                config_path = os.path.join(os.path.dirname(__file__), 'config.py')
                if os.path.exists(config_path):
                    with open(config_path, 'r', encoding='utf-8') as f:
                        lines = f.readlines()
                    
                    updated = False
                    for i, line in enumerate(lines):
                        if line.strip().startswith('MACHINE_NO ='):
                            indent = line[:len(line) - len(line.lstrip())]
                            lines[i] = f'{indent}MACHINE_NO = "{new_name}"\n'
                            updated = True
                            break
                    
                    if updated:
                        with open(config_path, 'w', encoding='utf-8') as f:
                            f.writelines(lines)
                        print(f"[SYSTEM] Machine name updated and saved to config.py: {new_name}")
                    else:
                        print("[WARN] Could not find MACHINE_NO line in config.py to update")
                else:
                    print(f"[WARN] config.py not found at path: {config_path}")

                return jsonify({"status": "success", "machine_no": new_name})
            except Exception as e:
                print(f"[ERROR] Failed to update machine name: {e}")
                return jsonify({"status": "error", "message": str(e)}), 500

        @self.app.route('/api/system_log', methods=['POST'])
        def api_system_log_create():
            try:
                data = request.json or {}
                employee_id = (data.get('employee_id') or '').strip().upper()
                action = (data.get('action') or '').strip()
                ip = request.remote_addr
                if not employee_id or not action:
                    return jsonify({"status": "error", "message": "employee_id and action are required"}), 400
                
                ok = DatabaseManager.store_system_log(employee_id, action, ip)
                return jsonify({"status": "success" if ok else "error"})
            except Exception as e:
                return jsonify({"status": "error", "message": str(e)}), 500

        @self.app.route('/api/system_log', methods=['GET'])
        def api_system_log_list():
            try:
                page = int(request.args.get('page', 1))
                employee_id = request.args.get('employee_id')
                action = request.args.get('action')
                date = request.args.get('date')

                # --- [COMMENTED OUT] Mock up Data of Setting Log (Disabled as requested) ---
                # if getattr(Config, 'MOCKUP_MODE', False):
                #     actions = ['login', 'logout', 'reset_ip_address', 'reset_logs', 'system_reset', 'reset_rfid_settings', 'update_machine_name']
                #     employees = ['ADMIN', '13991628', '13989472', '14001234']
                #     from datetime import datetime, timedelta
                #     fake_logs = []
                #     # Generate 30 logs for testing pagination
                #     for i in range(1, 31):
                #         emp = employees[i % len(employees)]
                #         act = actions[i % len(actions)]
                #         dt = datetime.now() - timedelta(minutes=i * 10)
                #         fake_logs.append((i, emp, act, dt, '127.0.0.1'))
                #     
                #     filtered = []
                #     for r in fake_logs:
                #         if employee_id and employee_id.lower() not in r[1].lower(): continue
                #         if action and action.lower() not in r[2].lower(): continue
                #         if date and r[3].strftime('%Y-%m-%d') != date: continue
                #         filtered.append(r)
                #         
                #     total = len(filtered)
                #     page_size = getattr(Config, 'PAGE_SIZE', 15)
                #     pages = (total + page_size - 1) // page_size
                #     offset = (page - 1) * page_size
                #     rows = filtered[offset : offset + page_size]
                #     
                #     logs = [{
                #         "id": r[0],
                #         "employeeId": r[1],
                #         "action": r[2],
                #         "timestamp": r[3].strftime("%Y-%m-%d %H:%M:%S"),
                #         "ip": r[4]
                #     } for r in rows]
                #     
                #     return jsonify({
                #         "status": "success",
                #         "logs": logs,
                #         "total": total,
                #         "pages": pages,
                #         "page": page
                #     })

                # --- [COMMENTED OUT] Real database audit logs query ---
                # data = DatabaseManager.get_system_logs(page, Config.PAGE_SIZE, employee_id, action, date)
                # data.update({"status": "success", "page": page})
                # return jsonify(data)

                data = DatabaseManager.get_system_logs(page, Config.PAGE_SIZE, employee_id, action, date)
                data.update({"status": "success", "page": page})
                return jsonify(data)
            except Exception as e:
                return jsonify({"status": "error", "message": str(e)}), 500

        @self.app.route('/api/check_employee', methods=['POST'])
        def api_check_employee():
            data = request.json or {}
            emp_id = (data.get('employee_id') or '').strip().upper()
            if not emp_id:
                return jsonify({"status": "error", "message": "No ID"}), 400
            if DatabaseManager.is_valid_employee(emp_id):
                return jsonify({"status": "success"})
            return jsonify({"status": "error", "message": "Invalid ID"}), 403

        @self.app.route("/data")
        def data():
            # --- [NEW] Mockup Mode robot location data ---
            # If MOCKUP_MODE is enabled, calculate moving mockup coordinates
            # to show active AGV icons moving on the live map canvas.
            if getattr(Config, 'MOCKUP_MODE', False):
                import math
                t = time.time()
                # Circular movement path simulation
                r1_x = 450 + 20 * math.sin(t / 10.0)
                r1_y = 150 + 20 * math.cos(t / 10.0)
                r2_x = 200 + 15 * math.sin(t / 7.0)
                r2_y = 350 + 15 * math.cos(t / 7.0)
                r3_x = 600 + 25 * math.sin(t / 12.0)
                r3_y = 250 + 25 * math.cos(t / 12.0)

                poses = [
                    {"ok": True, "name": "Robot FPC no.1", "pose": {"position": {"x": r1_x, "y": r1_y}, "orientation": 0.0}, "color": "#00e0ff"},
                    {"ok": True, "name": "Robot FPC no.2", "pose": {"position": {"x": r2_x, "y": r2_y}, "orientation": 0.0}, "color": "#ff6b6b"},
                    {"ok": True, "name": "Robot Cassette no.3", "pose": {"position": {"x": r3_x, "y": r3_y}, "orientation": 0.0}, "color": "#22c55e"}
                ]
                return jsonify({"robots": poses})

            # --- [COMMENTED OUT] Real hardware robot poses retrieval ---
            # poses = []
            # with ThreadPoolExecutor(max_workers=min(8, len(ROBOTS))) as ex:
            #     futures = {ex.submit(fetch_pose, r): r for r in ROBOTS}
            #     for fut in as_completed(futures):
            #         poses.append(fut.result())
            # return jsonify({"robots": poses})
            return jsonify({"robots": []})
        
        @self.app.route('/vk.css')
        def serve_vk_css():
            return send_from_directory('.', 'vk.css')

        @self.app.route('/vk.js')
        def serve_vk_js():
            return send_from_directory('.', 'vk.js')
        

        @self.app.route("/settings")
        def settings():
            # Pass role to front-end for UI lock (blur/disable), but server still enforces.
            return render_template("index.html", role=current_role())


        # -----------------------------
        # API: DB-backed login
        # -----------------------------
        @self.app.post("/api/login")
        def api_login():
            """
            Validates Employee ID against your DB and assigns role via Config.ADMIN_IDS.
            Sets session['employee_id'] and session['role'] for server-side enforcement.
            Front-end uses the returned role to blur/disable admin-only buttons.
            """
            data = request.get_json(silent=True) or {}
            employee_id = (data.get("employeeId") or "").strip().upper()

            if not employee_id:
                return jsonify({"ok": False, "error": "EMPTY_ID"}), 400

            # --- [NEW] Bypass DB validation for Admin & Specific Employee ID ---
            # We allow case-insensitive "ADMIN" and "13991628" to log in as administrators.
            # This ensures presentation/mockup logins work regardless of active database connection.
            is_mock_admin = employee_id in ["ADMIN", "13991628"]

            # --- [COMMENTED OUT] Old database check ---
            # if not DatabaseManager.is_valid_employee(employee_id):
            #     return jsonify({"ok": False, "error": "NOT_FOUND"}), 401

            if not is_mock_admin:
                try:
                    if not DatabaseManager.is_valid_employee(employee_id):
                        return jsonify({"ok": False, "error": "NOT_FOUND"}), 401
                except Exception as e:
                    print("[LOGIN] DB validation error (DB offline):", e)
                    return jsonify({"ok": False, "error": "NOT_FOUND"}), 401

            # Decide role by allowlist
            # --- [COMMENTED OUT] Old admin role mapping ---
            # role = "admin" if employee_id in getattr(Config, "ADMIN_IDS", set()) else "user"
            role = "admin" if (is_mock_admin or employee_id in getattr(Config, "ADMIN_IDS", [])) else "user"

            # Persist to session for server-side protection
            session["employee_id"] = employee_id
            session["role"] = role

            # --- [NEW] Record login event to system_log database ---
            try:
                ip_addr = request.headers.get('X-Forwarded-For', request.remote_addr)
                DatabaseManager.store_system_log(employee_id, "login", ip_addr)
            except Exception as e:
                print(f"[SYSTEM LOG ERROR] login: {e}")

            return jsonify({"ok": True, "employeeId": employee_id, "role": role})
        
        @self.app.post("/api/logout")
        def api_logout():
            emp_id = session.get("employee_id", "UNKNOWN")
            try:
                ip_addr = request.headers.get('X-Forwarded-For', request.remote_addr)
                DatabaseManager.store_system_log(emp_id, "logout", ip_addr)
            except Exception as e:
                print(f"[SYSTEM LOG ERROR] logout: {e}")
            session.clear()
            return jsonify({"ok": True})


        # -----------------------------
        # API: Settings actions (server-side protected)
        # -----------------------------
        @self.app.post("/settings/reset-ip")
        @self.admin_required
        def reset_ip():
            emp_id = session.get("employee_id", "ADMIN")
            try:
                ip_addr = request.headers.get('X-Forwarded-For', request.remote_addr)
                DatabaseManager.store_system_log(emp_id, "reset_ip_address", ip_addr)
            except Exception as e:
                print(f"[SYSTEM LOG ERROR] reset_ip: {e}")
            return jsonify({"ok": True, "message": "IP reset triggered"})

        @self.app.post("/settings/reset-logs")
        @self.admin_required
        def reset_logs():
            emp_id = session.get("employee_id", "ADMIN")
            try:
                ip_addr = request.headers.get('X-Forwarded-For', request.remote_addr)
                DatabaseManager.store_system_log(emp_id, "reset_logs", ip_addr)
            except Exception as e:
                print(f"[SYSTEM LOG ERROR] reset_logs: {e}")
            return jsonify({"ok": True, "message": "Log reset triggered"})

        @self.app.post("/settings/system-reset")
        @self.admin_required
        def system_reset():
            emp_id = session.get("employee_id", "ADMIN")
            try:
                ip_addr = request.headers.get('X-Forwarded-For', request.remote_addr)
                DatabaseManager.store_system_log(emp_id, "system_reset", ip_addr)
            except Exception as e:
                print(f"[SYSTEM LOG ERROR] system_reset: {e}")
            return jsonify({"ok": True, "message": "System reset triggered"})

        @self.app.post("/settings/reset-rfid")
        def reset_rfid():
            emp_id = session.get("employee_id", "ADMIN")
            try:
                ip_addr = request.headers.get('X-Forwarded-For', request.remote_addr)
                DatabaseManager.store_system_log(emp_id, "reset_rfid_settings", ip_addr)
            except Exception as e:
                print(f"[SYSTEM LOG ERROR] reset_rfid: {e}")
            return jsonify({"ok": True, "message": "RFID settings reset"})

        @self.app.get("/whoami")
        def whoami():
            role = session.get("role", "user")
            logged_in = bool(session.get("employee_id"))
            return jsonify({"ok": True, "role": role, "loggedIn": logged_in})




    def _get_current_data(self):
        """
        Returns the snapshot used by Home/AGV pages (merged: HEADER + FPC live).
        No auto-enrichment here; coordinator handles scan_log.
        """
        try:
            # --- [NEW] Mockup Mode handler check ---
            # If MOCKUP_MODE is enabled in configuration, we skip physical RFID readers
            # and directly return the simulated data loop state.
            if getattr(Config, 'MOCKUP_MODE', False):
                agv = request.args.get('agv')
                if agv == 'agv1':
                    mock_agv = getattr(self, '_live_mock_agv1', {'reader_connected': True, 'data': {}})
                    return jsonify({
                        'status': 'success',
                        'reader_connected': mock_agv.get('reader_connected', True),
                        'data': mock_agv.get('data', {}),
                        'mockup_mode': True
                    })
                elif agv == 'agv2':
                    mock_agv = getattr(self, '_live_mock_agv2', {'reader_connected': True, 'data': {}})
                    return jsonify({
                        'status': 'success',
                        'reader_connected': mock_agv.get('reader_connected', True),
                        'data': mock_agv.get('data', {}),
                        'mockup_mode': True
                    })
                elif agv == 'agv3':
                    mock_agv = getattr(self, '_live_mock_agv3', {'cassette_connected': True, 'cassette1': {}, 'cassette2': {}})
                    return jsonify({
                        'status': 'success',
                        'cassette_connected': mock_agv.get('cassette_connected', True),
                        'cassette1': mock_agv.get('cassette1', {}),
                        'cassette2': mock_agv.get('cassette2', {}),
                        'mockup_mode': True
                    })

                # Default (Home page)
                current = getattr(self, '_live_mock_fpc', {}).copy()
                is_connected = current.get('reader_connected', True)
                cass_state = getattr(self, '_live_mock_cassette', self._cassette_state).copy()

                rfid_status = {
                    'fpc': {
                        'connected': is_connected,
                        'sensor': 'ON' if (is_connected and not current.get('fpc_id')) else 'OFF'
                    },
                    'cassette': {
                        'connected': is_connected,
                        'sensor': 'ON' if is_connected else 'OFF'
                    },
                    'header': {
                        'connected': is_connected,
                        'sensor': 'ON' if is_connected else 'OFF'
                    }
                }

                # --- [NEW] Include machine_no in mockup mode response ---
                return jsonify({
                    'status': 'success',
                    'reader_connected': is_connected,
                    'data': current,
                    'cassette_connected': is_connected,
                    'cassette': cass_state,
                    'machine_no': getattr(Config, 'MACHINE_NO', '-'),
                    'mockup_mode': True,
                    'rfid_status': rfid_status
                })

            # --- connectivity: check if any active reader is connected ---
            hdr_connected = False
            try:
                if getattr(self, 'header_reader', None):
                    hdr_connected = bool(self.header_reader.is_hw_connected())
                    if hdr_connected and not self.header_reader.running:
                        print("[AUTO] Header reader detected, starting thread...")
                        self.header_reader.start_reading()
                    elif not hdr_connected and self.header_reader.running:
                        self.header_reader.running = False
            except Exception as e:
                print(f"[WARN] header is_hw_connected failed: {e}")
                hdr_connected = False

            fpc_connected = False
            try:
                if getattr(self, 'fpc_reader', None):
                    fpc_connected = bool(self.fpc_reader.is_hw_connected())
            except Exception:
                fpc_connected = False

            is_connected = hdr_connected or fpc_connected

            # --- base snapshot ---
            current = {
                'timestamp': None,
                'machine_no': getattr(Config, 'MACHINE_NO', '-'),
                'agv_no': getattr(Config, 'AGV_NO', None),
                'header_name': None,
                'header_id': None,
                'fpc_id': None,
                'batch_id': None,
                'lot_id': None,
                'touchdown': None,
                'pm_date': None,
                'comment': None,
            }


            # --- merge FPC live state ---
            try:
                if getattr(self, 'fpc_reader', None):
                    f = self.fpc_reader.snapshot()
                    if isinstance(f, dict) and f.get('fpc_id'):
                        current['fpc_id'] = f['fpc_id']
                        if not current['timestamp'] and f.get('timestamp'):
                            current['timestamp'] = f['timestamp']
            except Exception as e:
                print("[WARN] merge FPC snapshot failed:", e)

            # --- merge HEADER live state ---
            try:
                if getattr(self, 'header_reader', None):
                    hdr = self.header_reader.get_current_data()
                    if isinstance(hdr, dict):
                        h_id = hdr.get('header_id')
                        h_name = hdr.get('header_name')
                        h_ts = hdr.get('timestamp')

                        if h_id:
                            current['header_id'] = h_id
                            current['header_name'] = h_name
                            if not current['timestamp'] and h_ts:
                                current['timestamp'] = h_ts
            except Exception as e:
                print("[WARN] merge HEADER snapshot failed:", e)
            # ---- Phase 3: overlay pair status + enrichment into the live snapshot ----
            try:
                ps = getattr(self, '_pair_state', {}) or {}
                # only overlay if the cached pair matches what we currently display
                if ps and ps.get('header_id') and ps.get('fpc_id'):
                    if (current.get('header_id') == ps['header_id']) and (current.get('fpc_id') == ps['fpc_id']):
                        # expose status for the frontend
                        current['pair_ok'] = bool(ps.get('pair_ok'))
                        current['pair_status'] = ps.get('pair_status')
                        if ps.get('pair_ok'):
                            # only when active: surface enrichment
                            current['batch_id']  = ps.get('batch_id')
                            current['lot_id']    = ps.get('lot_id')
                            current['touchdown'] = ps.get('touchdown')
                            current['pm_date']   = ps.get('pm_date')
                            current['comment']   = ps.get('comment')
                            # Expose mismatch data for frontend
                            current['mismatch_detected'] = bool(ps.get('mismatch_detected'))
                            current['mismatch_type'] = ps.get('mismatch_type')
                            current['mismatch_header'] = ps.get('mismatch_header')
                            current['mismatch_fpc'] = ps.get('mismatch_fpc')
                    else:
                        # pair cache doesn't match the live pair; don't overlay
                        current['pair_ok'] = None
                        current['pair_status'] = None
                else:
                    current['pair_ok'] = None
                    current['pair_status'] = None
            except Exception as e:
                print("[WARN] overlay pair/enrichment failed:", e)
            # If we have both IDs, send explicit match flags for the UI
            hdr = current.get('header_id')
            fpc = current.get('fpc_id')

            # [RF CROSSTALK FILTER] Header and FPC cannot be the same physical tag UID
            if hdr and fpc and hdr.strip().lower() == fpc.strip().lower():
                if DatabaseManager.is_known_fpc_tag(hdr):
                    hdr = current['header_id'] = None
                elif DatabaseManager.is_known_header_tag(fpc):
                    fpc = current['fpc_id'] = None

            if hdr and fpc:
                # =============================================================================
                # 🔍 CONFIRM DATA: LIVE CHECK WITH smart_store_probe_card
                # =============================================================================
                confirm_res = DatabaseManager.confirm_probe_card_data(fpc, hdr)
                if confirm_res.get('status') == 'MATCH_OK':
                    current['pair_ok'] = True
                    current['match_ok'] = True
                    current['allowed'] = True
                    current['active'] = True
                    current['active_pair'] = True
                    # Expose match data for frontend ONLY when BOTH match
                    current['touchdown'] = confirm_res.get('touchdown')
                    current['pm_date'] = confirm_res.get('pm_date')
                    current['comment'] = confirm_res.get('comment')
                    current['mismatch_detected'] = False
                    current['mismatch_type'] = None
                    current['mismatch_message'] = None
                else:
                    current['pair_ok'] = False
                    current['match_ok'] = False
                    current['allowed'] = False
                    current['active'] = False
                    current['active_pair'] = False
                    current['touchdown'] = None
                    current['pm_date'] = None
                    current['comment'] = None
                    current['mismatch_detected'] = True
                    current['mismatch_type'] = confirm_res.get('mismatch_type', 'not_allowed')
                    current['mismatch_message'] = confirm_res.get('mismatch_message')
                    current['mismatch_reason'] = confirm_res.get('mismatch_message')
                    current['mismatch_header'] = hdr
                    current['mismatch_fpc'] = fpc
            else:
                # Either only FPC or only Header is present (or none)
                # Touchdown, PM Date, Comment MUST remain empty (None) until both are matched!
                current['touchdown'] = None
                current['pm_date'] = None
                current['comment'] = None
                current['pair_ok'] = None
                current['match_ok'] = None
                current['active_pair'] = None
                current['mismatch_detected'] = False
                current['mismatch_type'] = None
                current['mismatch_message'] = None
                if not fpc and not hdr:
                    self._last_logged_live_pair = None

            # --- Check cassette reader hardware state ---
            is_cassette_connected = is_cassette_hw_connected()
            try:
                if getattr(self, 'cassette_reader', None):
                    if is_cassette_connected and not self.cassette_reader.running and not getattr(self.cassette_reader, '_started_once', False):
                        self.cassette_reader._started_once = True
                        self.cassette_reader.start_reading()
                    elif not is_cassette_connected and self.cassette_reader.running:
                        self.cassette_reader.running = False
            except Exception as e:
                is_cassette_connected = False

            # Live cassette state is managed authoritatively by _on_cassette_scan & _cassette_timer_loop
            # We also check if cassette_reader (serial fallback) has an active tag
            try:
                if getattr(self, 'cassette_reader', None):
                    cass = self.cassette_reader.get_current_data()
                    if isinstance(cass, dict) and cass.get('cassette_id'):
                        c_id = cass.get('cassette_id')
                        if self._cassette_state.get('cassette_id') != c_id:
                            self._on_cassette_scan(c_id)
            except Exception as e:
                print("[WARN] merge Cassette snapshot failed:", e)

            # RFID-2 (FPC) sensor status
            fpc_sensor_val = "OFF"
            if getattr(self, 'fpc_reader', None) and hasattr(self.fpc_reader, 'sensor') and self.fpc_reader.sensor:
                try:
                    fpc_sensor_val = "ON" if self.fpc_reader.sensor.is_active() else "OFF"
                except Exception:
                    fpc_sensor_val = "OFF"

            rfid_status = {
                'fpc': {
                    'connected': bool(self.fpc_reader.is_hw_connected()) if getattr(self, 'fpc_reader', None) else False,
                    'sensor': fpc_sensor_val
                },
                'cassette': {
                    'connected': is_cassette_connected,
                    'sensor': 'ON'
                },
                'header': {
                    'connected': bool(self.header_reader.is_hw_connected()) if getattr(self, 'header_reader', None) else False,
                    'sensor': 'ON'
                }
            }

            # --- [NEW] Include machine_no in non-mockup response ---
            return jsonify({
                'status': 'success',
                'reader_connected': is_connected,
                'data': current,
                'cassette_connected': is_cassette_connected or getattr(self, '_cassette_simulated_connected', False),
                'cassette': self._cassette_state,
                'machine_no': getattr(Config, 'MACHINE_NO', '-'),
                'rfid_status': rfid_status
            })

        except Exception as e:
            print(f"[ERROR] _get_current_data:", e)
            return jsonify({'status': 'error', 'message': 'Failed to get current data'}), 500


    def _mockup_simulator_loop(self):
        """
        --- [NEW] Mockup Mode simulator loop ---
        Runs as a background thread to rotate mockup RFID scan data.
        This updates Home page cards and all three AGV dashboard states.
        It cycles through 3 connection states:
        0: Connected & Tag Active (Green)
        1: Connected & No Tag Detected (Grey)
        2: Disconnected (Red)
        """
        cycle = 0
        
        while True:
            try:
                if getattr(Config, 'MOCKUP_MODE', False):
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    mode = cycle % 4  # 0: Green, 1: Yellow, 2: Grey, 3: Red
                    
                    if mode == 0:
                        # === STATE 0: Connected, Tag Active (Green) ===
                        # Live FPC card scanned on Home page (normal touchdown)
                        self._live_mock_fpc = {
                            'timestamp': now_str,
                            'machine_no': 'AVT#55',
                            'agv_no': None,
                            'header_name': 'Header FPC 11',
                            'header_id': 'H13080-PHS-11',
                            'fpc_id': 'P13080-FHB-0364',
                            'batch_id': None,
                            'lot_id': None,
                            'touchdown': 12000,
                            'pm_date': '2025-07-12 16:00:00',
                            'comment': 'CLEAN ; ; Testfile No PM PASS/OFF LINE CLEANING/ QC PASS H=322UM/CPASS/20090',
                            'reader_connected': True,
                            'pair_ok': True,
                            'allowed': True,
                            'active': True,
                            'match_ok': True
                        }
                        # Live Cassette card scanned on Home page
                        self._live_mock_cassette = {
                            'cassette_id': 'CASS-001',
                            'machine_status': 'RUNNING',
                            'lot_id': 'LOT-X88',
                            'batch_id': 'BATCH-B01',
                            'last_cleaning': '2025-10-01 08:00:00',
                            'next_cleaning': '2025-10-15 08:00:00',
                            'timestamp': now_str
                        }
                        # Simulated active state for AGV 1
                        self._live_mock_agv1 = {
                            'reader_connected': True,
                            'data': {
                                'fpc_id': 'P13080-FHB-0364',
                                'header_id': 'H13080-PHS-11',
                                'pm_date': '2025-07-12 16:00:00',
                                'timestamp': 'BATCH-001'
                            }
                        }
                        # Simulated active state for AGV 2
                        self._live_mock_agv2 = {
                            'reader_connected': True,
                            'data': {
                                'fpc_id': '2ID031FV002B',
                                'header_id': 'H15230-PHS-03',
                                'pm_date': '2025-09-29 16:53:21',
                                'timestamp': 'BATCH-111'
                            }
                        }
                        # Simulated active state for AGV 3 (Slot 1 & 2 populated)
                        self._live_mock_agv3 = {
                            'cassette_connected': True,
                            'cassette1': {
                                'cassette_id': 'CASS-001',
                                'machine_status': 'RUNNING',
                                'lot_id': 'LOT-X88',
                                'batch_id': 'BATCH-B01',
                                'timestamp': now_str
                            },
                            'cassette2': {
                                'cassette_id': 'CASS-002',
                                'machine_status': 'CLEANING',
                                'lot_id': 'LOT-Y99',
                                'batch_id': 'BATCH-B02',
                                'timestamp': now_str
                            }
                        }
                    elif mode == 1:
                        # === STATE 1: Connected, Tag Active (Yellow / PM Warning) ===
                        # Live FPC card scanned on Home page (touchdown limit exceeded)
                        self._live_mock_fpc = {
                            'timestamp': now_str,
                            'machine_no': 'AVT#55',
                            'agv_no': None,
                            'header_name': 'Header FPC 11',
                            'header_id': 'H13080-PHS-11',
                            'fpc_id': 'P13080-FHB-0364',
                            'batch_id': None,
                            'lot_id': None,
                            'touchdown': 62000,
                            'pm_date': '2025-07-12 16:00:00',
                            'comment': 'CLEAN ; ; Testfile No PM PASS/OFF LINE CLEANING/ QC PASS H=322UM/CPASS/20090',
                            'reader_connected': True,
                            'pair_ok': True,
                            'allowed': True,
                            'active': True,
                            'match_ok': True
                        }
                        # Live Cassette card scanned on Home page
                        self._live_mock_cassette = {
                            'cassette_id': 'CASS-001',
                            'machine_status': 'RUNNING',
                            'lot_id': 'LOT-X88',
                            'batch_id': 'BATCH-B01',
                            'last_cleaning': '2025-10-01 08:00:00',
                            'next_cleaning': '2025-10-15 08:00:00',
                            'timestamp': now_str
                        }
                        # Simulated active state for AGV 1
                        self._live_mock_agv1 = {
                            'reader_connected': True,
                            'data': {
                                'fpc_id': 'P13080-FHB-0364',
                                'header_id': 'H13080-PHS-11',
                                'pm_date': '2025-07-12 16:00:00',
                                'timestamp': 'BATCH-001'
                            }
                        }
                        # Simulated active state for AGV 2
                        self._live_mock_agv2 = {
                            'reader_connected': True,
                            'data': {
                                'fpc_id': '2ID031FV002B',
                                'header_id': 'H15230-PHS-03',
                                'pm_date': '2025-09-29 16:53:21',
                                'timestamp': 'BATCH-111'
                            }
                        }
                        # Simulated active state for AGV 3 (Slot 1 & 2 populated)
                        self._live_mock_agv3 = {
                            'cassette_connected': True,
                            'cassette1': {
                                'cassette_id': 'CASS-001',
                                'machine_status': 'RUNNING',
                                'lot_id': 'LOT-X88',
                                'batch_id': 'BATCH-B01',
                                'timestamp': now_str
                            },
                            'cassette2': {
                                'cassette_id': 'CASS-002',
                                'machine_status': 'CLEANING',
                                'lot_id': 'LOT-Y99',
                                'batch_id': 'BATCH-B02',
                                'timestamp': now_str
                            }
                        }
                    elif mode == 2:
                        # === STATE 2: Connected, No Tag (Grey) ===
                        # Live FPC reader online, but no tag placed
                        self._live_mock_fpc = {
                            'timestamp': now_str,
                            'machine_no': 'AVT#55',
                            'agv_no': None,
                            'header_name': None,
                            'header_id': None,
                            'fpc_id': None,
                            'batch_id': None,
                            'lot_id': None,
                            'touchdown': None,
                            'pm_date': None,
                            'comment': None,
                            'reader_connected': True,
                            'pair_ok': None,
                            'allowed': None,
                            'active': None,
                            'match_ok': None
                        }
                        # Live Cassette reader online, but no tag placed
                        self._live_mock_cassette = {
                            'cassette_id': None,
                            'machine_status': None,
                            'lot_id': None,
                            'batch_id': None,
                            'last_cleaning': None,
                            'next_cleaning': None,
                            'timestamp': now_str
                        }
                        # AGV 1 reader online, no tag
                        self._live_mock_agv1 = {
                            'reader_connected': True,
                            'data': {}
                        }
                        # AGV 2 reader online, no tag
                        self._live_mock_agv2 = {
                            'reader_connected': True,
                            'data': {}
                        }
                        # AGV 3 reader online, no cassettes
                        self._live_mock_agv3 = {
                            'cassette_connected': True,
                            'cassette1': {},
                            'cassette2': {}
                        }
                    else:
                        # === STATE 3: Disconnected (Red) ===
                        # FPC reader is offline
                        self._live_mock_fpc = {
                            'timestamp': now_str,
                            'machine_no': 'AVT#55',
                            'agv_no': None,
                            'header_name': None,
                            'header_id': None,
                            'fpc_id': None,
                            'batch_id': None,
                            'lot_id': None,
                            'touchdown': None,
                            'pm_date': None,
                            'comment': None,
                            'reader_connected': False,
                            'pair_ok': None,
                            'allowed': None,
                            'active': None,
                            'match_ok': None
                        }
                        # Cassette reader is offline
                        self._live_mock_cassette = {
                            'cassette_id': None,
                            'machine_status': None,
                            'lot_id': None,
                            'batch_id': None,
                            'last_cleaning': None,
                            'next_cleaning': None,
                            'timestamp': now_str
                        }
                        # AGV 1 reader is offline
                        self._live_mock_agv1 = {
                            'reader_connected': False,
                            'data': {}
                        }
                        # AGV 2 reader is offline
                        self._live_mock_agv2 = {
                            'reader_connected': False,
                            'data': {}
                        }
                        # AGV 3 reader is offline
                        self._live_mock_agv3 = {
                            'cassette_connected': False,
                            'cassette1': {},
                            'cassette2': {}
                        }
                    
                    cycle += 1
            except Exception as e:
                print("[MOCK] Simulator loop error:", e)
            time.sleep(15)


    def _reader_watchdog(self):
        """Background thread that (re)starts readers when USB devices are present"""
        while True:
            try:
                # Header reader
                if self.header_reader:
                    try:
                        present = self.header_reader.is_hw_connected()
                        if present and not self.header_reader.running:
                            print("[WATCHDOG] USB present; starting HEADER reader")
                            self.header_reader.start_reading()
                    except Exception as e:
                        print("[WATCHDOG] header error:", e)

                # FPC reader (auto-recover)
                if self.fpc_reader:
                    try:
                        present = self.fpc_reader.is_hw_connected()
                        if present and not self.fpc_reader.running:
                            print("[WATCHDOG] USB present; starting FPC reader")
                            self.fpc_reader.start()
                    except Exception as e:
                        print("[WATCHDOG] fpc error:", e)

                # Cassette reader (auto-recover)
                if self.cassette_reader:
                    try:
                        present = self.cassette_reader.is_hw_connected()
                        if present and not self.cassette_reader.running and not getattr(self.cassette_reader, '_failed_once', False):
                            print("[WATCHDOG] USB present; starting CASSETTE reader")
                            ok = self.cassette_reader.start_reading()
                            if not ok:
                                self.cassette_reader._failed_once = True
                    except Exception as e:
                        pass
            except Exception as e:
                print("[WATCHDOG] loop error:", e)

            time.sleep(1)

    # ============================================================================
    # CASSETTE STATE MACHINE & EVENT HANDLER
    # ============================================================================
    @staticmethod
    def _convert_thai_kedmanee_to_en(text):
        if not text:
            return ""
        thai_to_en = {
            'ๆ':'q','ไ':'w','ำ':'e','พ':'r','ะ':'t','ั':'y','ี':'u','ร':'i','น':'o','ย':'p','บ':'[','ล':']',
            'ฟ':'a','ห':'s','ก':'d','ด':'f','เ':'g','้':'h','่':'j','า':'k','ส':'l','ว':';','ง':'\'',
            'ผ':'z','ป':'x','แ':'c','อ':'v','ิ':'b','ื':'n','ท':'m','ม':',','ใ':'.','ฝ':'/',
            '๑':'@','๒':'#','๓':'$','๔':'%','๕':'&','๖':'_','๗':'+','๘':'*','๙':'(','๐':')',
            'ๅ':'1','ภ':'4','ถ':'5','ุ':'6','ึ':'7','ค':'8','ต':'9','จ':'0','ข':'-','ช':'='
        }
        return ''.join(thai_to_en.get(ch, ch) for ch in text).strip()

    def _on_cassette_scan(self, tag):
        tag = self._convert_thai_kedmanee_to_en((tag or "").strip())
        if not tag:
            return {"status": "error", "message": "empty tag"}
        
        now = time.time()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Case 1: Same tag scanned while already LOADED (continuous read at dock)
        if self._cassette_stage == "LOADED" and tag.lower() == str(self._cassette_active_tag or "").lower():
            self._cassette_last_seen = now
            return {"status": "success", "message": "Cassette at dock (continuous read)", "data": self._cassette_state}

        # Case 2: Tag comes back after being IN_PROCESS (Step 3: Test Finished / Standby at dock)
        if self._cassette_stage == "IN_PROCESS" and tag.lower() == str(self._cassette_active_tag or "").lower():
            self._cassette_stage = "STANDBY"
            self._cassette_last_seen = now
            self._cassette_state["stage"] = "STANDBY"
            print(f"[CASSETTE] Tag {tag} returned from Prober machine -> Stage: STANDBY")
            # Log standby completion
            details = DatabaseManager.get_cassette_details(tag) or {}
            DatabaseManager.store_cassette_log(
                tag,
                "STANDBY_COMPLETED",
                self._cassette_state.get('lot_id'),
                self._cassette_state.get('batch_id'),
                details.get('last_cleaning'),
                details.get('next_cleaning'),
                timestamp
            )
            return {"status": "success", "message": "Cassette returned to dock (Standby)", "data": self._cassette_state}

        # Case 3: New tag arrived (Step 1: First Arrival at dock)
        self._cassette_active_tag = tag
        self._cassette_stage = "LOADED"
        self._cassette_last_seen = now
        self._cassette_state.update({
            "cassette_id": tag,
            "machine_status": "Active",
            "lot_id": tag,
            "batch_id": tag,
            "last_cleaning": None,
            "next_cleaning": None,
            "timestamp": timestamp,
            "stage": "LOADED"
        })
        print(f"[CASSETTE] Read Raw Tag: {tag}")
        DatabaseManager.store_cassette_log(
            tag,
            "LOADED",
            tag,
            tag,
            None,
            None,
            timestamp
        )
        return {"status": "success", "message": f"Cassette tag {tag} read", "data": self._cassette_state}

    def _cassette_timer_loop(self):
        """
        Watchdog timer loop for Cassette state transitions:
        - If LOADED and not seen for >= 2 mins (120s) -> Transition to IN_PROCESS (Inside prober)
        - If STANDBY and not seen for >= 1 min (60s) -> Clear screen to IDLE (Cassette removed)
        """
        in_process_timeout = getattr(Config, 'CASSETTE_IN_PROCESS_TIMEOUT_S', 120)
        clear_timeout = getattr(Config, 'CASSETTE_CLEAR_TIMEOUT_S', 60)

        while True:
            try:
                now = time.time()
                # Step 2: If LOADED and not seen for >= 2 minutes -> Mark as IN_PROCESS
                if self._cassette_stage == "LOADED" and self._cassette_last_seen > 0:
                    if (now - self._cassette_last_seen) >= in_process_timeout:
                        self._cassette_stage = "IN_PROCESS"
                        self._cassette_state["stage"] = "IN_PROCESS"
                        print(f"[CASSETTE] >{in_process_timeout}s without dock read -> Transitioned to IN_PROCESS for tag {self._cassette_active_tag}")

                # Step 4: If STANDBY and not seen for >= 1 minute -> Cleared / IDLE
                elif self._cassette_stage == "STANDBY" and self._cassette_last_seen > 0:
                    if (now - self._cassette_last_seen) >= clear_timeout:
                        print(f"[CASSETTE] >{clear_timeout}s without read after STANDBY -> Box unloaded, clearing screen to IDLE")
                        self._cassette_stage = "IDLE"
                        self._cassette_active_tag = None
                        self._cassette_last_seen = 0
                        for k in self._cassette_state:
                            self._cassette_state[k] = None
                        self._cassette_state["stage"] = "IDLE"
            except Exception as e:
                print(f"[CASSETTE TIMER ERROR] {e}")
            time.sleep(1)

    def _both_logger_loop(self):
        """
        Commit one scan_log row after the FPC reader stops reading (sensor LOW):
        - While FPC window is OPEN, remember the latest header seen.
        - When FPC window CLOSES due to sensor LOW *and a tag was held*,
          insert exactly one scan_log using (cached_header, last_window_fpc_id).
        """
        gap = 0.2
        while True:
            try:
                hdr_id_now = None
                fpc_window_open = False
                fpc_closed_flag = False
                fpc_last_id = None
                ts_now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

                if self.header_reader and isinstance(self.header_reader.get_current_data(), dict):
                    hdr_id_now = self.header_reader.get_current_data().get('header_id')

                if self.fpc_reader:
                    fpc_window_open = bool(getattr(self.fpc_reader, 'window_open', False))
                    fpc_closed_flag = bool(getattr(self.fpc_reader, 'window_just_closed', False))
                    fpc_last_id = getattr(self.fpc_reader, 'last_window_fpc_id', None)

                # While window is open, keep caching the latest header_id
                if fpc_window_open and hdr_id_now:
                    self._hdr_seen_in_window = hdr_id_now

                # If the window just closed (sensor LOW) *and we had a tag*, do one insert
                if fpc_closed_flag and fpc_last_id:
                    # Clear flag immediately
                    if self.fpc_reader:
                        self.fpc_reader.window_just_closed = False

                    hdr_for_commit = self._hdr_seen_in_window or hdr_id_now

                    if hdr_for_commit:
                        # [RF Crosstalk Guard] Ignore if exact same tag UID read on both
                        if fpc_last_id.strip().lower() != hdr_for_commit.strip().lower():
                            confirm_res = DatabaseManager.confirm_probe_card_data(fpc_last_id, hdr_for_commit)

                            if confirm_res.get("status") == "MATCH_OK":
                                td_val = confirm_res.get("touchdown")
                                pm_val = confirm_res.get("pm_date")
                                cm_val = confirm_res.get("comment") or "MATCH_OK"
                                src = 'MATCH_OK'
                                self._pair_state.update({
                                    "pair_ok": True,
                                    "pair_status": "MATCH_OK",
                                    "match_ok": True,
                                    "header_id": hdr_for_commit,
                                    "fpc_id": fpc_last_id,
                                    "touchdown": td_val,
                                    "pm_date": pm_val,
                                    "comment": cm_val,
                                    "ts": ts_now,
                                    "mismatch_detected": False,
                                    "mismatch_type": None,
                                    "mismatch_message": None
                                })
                                print(f"[CONFIRM DATA] Match OK! FPC: {fpc_last_id} + Header: {hdr_for_commit} (TD: {td_val}, PM: {pm_val})")
                            elif confirm_res.get("status") == "NOT_FOUND":
                                m_type = "not_found"
                                m_msg = confirm_res.get("mismatch_message", "Tag Not Found in Database")
                                td_val = None
                                pm_val = None
                                cm_val = m_msg
                                src = 'NOT_FOUND'
                                self._pair_state.update({
                                    "pair_ok": False,
                                    "pair_status": "NOT_FOUND",
                                    "match_ok": False,
                                    "header_id": hdr_for_commit,
                                    "fpc_id": fpc_last_id,
                                    "touchdown": None,
                                    "pm_date": None,
                                    "comment": None,
                                    "ts": ts_now,
                                    "mismatch_detected": True,
                                    "mismatch_type": "not_found",
                                    "mismatch_message": m_msg,
                                    "mismatch_header": hdr_for_commit,
                                    "mismatch_fpc": fpc_last_id
                                })
                                print(f"[CONFIRM DATA ALERT] NOT FOUND: {m_msg}")
                            else:
                                m_type = confirm_res.get("mismatch_type", "not_allowed")
                                m_msg = confirm_res.get("mismatch_message", "Header Mismatch")
                                td_val = None
                                pm_val = None
                                cm_val = m_msg
                                src = 'MISMATCH'
                                self._pair_state.update({
                                    "pair_ok": False,
                                    "pair_status": "MISMATCH",
                                    "match_ok": False,
                                    "header_id": hdr_for_commit,
                                    "fpc_id": fpc_last_id,
                                    "touchdown": None,
                                    "pm_date": None,
                                    "comment": None,
                                    "ts": ts_now,
                                    "mismatch_detected": True,
                                    "mismatch_type": m_type,
                                    "mismatch_message": m_msg,
                                    "mismatch_header": hdr_for_commit,
                                    "mismatch_fpc": fpc_last_id
                                })
                                print(f"[CONFIRM DATA ALERT] MISMATCH: {m_msg}")

                            # Insert ONE immutable log into scan_log
                            try:
                                DatabaseManager.store_scan_log(
                                    timestamp=ts_now,
                                    machine_no=getattr(Config, 'MACHINE_NO', '-'),
                                    agv_no=getattr(Config, 'AGV_NO', '-'),
                                    fpc_id=fpc_last_id,
                                    header_id=hdr_for_commit,
                                    header_name=None,
                                    batch_id=None,
                                    lot_id=None,
                                    source=src,
                                    touchdown=td_val,
                                    latest_pm=pm_val,
                                    comment=cm_val
                                )
                                print(f"[SCAN LOGGER] Log inserted ({src}): Header={hdr_for_commit}, FPC={fpc_last_id} @ {ts_now}")
                            except Exception as e:
                                print(f"[SCAN LOGGER] store_scan_log error: {e}")

            except Exception as e:
                print("[BOTH] loop error:", e)

            time.sleep(gap)

    def _get_logs(self):
        """Get paginated logs from snapshot table (scan_log)."""
        try:
            page = int(request.args.get('page', 1))
            offset = (page - 1) * Config.PAGE_SIZE

            conn = DatabaseManager.get_connection()
            cur = conn.cursor()

            # Get total rows count
            cur.execute("SELECT COUNT(*) FROM scan_log")
            total = cur.fetchone()[0]
            total_pages = (total + Config.PAGE_SIZE - 1) // Config.PAGE_SIZE

            # Get paginated rows with immutable source & comment
            cur.execute("""
                SELECT id, fpc_id, header_id, header_name, timestamp, agv_no, machine_no, batch_id, lot_id, source, touchdown, comment
                FROM scan_log
                ORDER BY timestamp DESC
                LIMIT %s OFFSET %s
            """, (Config.PAGE_SIZE, offset))
            
            rows = cur.fetchall()
            conn.close()

            logs = []
            for r in rows:
                f_id = r[1]
                h_id = r[2]
                source_val = str(r[9] or '').upper().strip()
                is_nf = (source_val == 'NOT_FOUND')
                is_mis = (source_val in ('MISMATCH', 'MISMATCH_DETECTED', 'NOT_ALLOWED'))
                res_type = 'not_found' if is_nf else ('mismatch' if is_mis else 'match')
                logs.append({
                    "logId":     f"LOG{str(r[0]).zfill(6)}",
                    "fpcId":     f_id,
                    "headerId":  h_id,
                    "headerName": r[3],
                    "timestamp": (r[4].strftime('%Y-%m-%d %H:%M:%S') if hasattr(r[4], 'strftime') else str(r[4])) if r[4] else None,
                    "agvNo":     r[5],
                    "machineNo": r[6],
                    "batchId":   r[7],
                    "lotId":     r[8],
                    "source":    source_val,
                    "resultType": res_type,
                    "touchdown": r[10],
                    "comment":   r[11],
                    "isMismatch": is_mis,
                })

            return jsonify({
                "status": "success",
                "logs": logs,
                "total": total,
                "page": page,
                "pages": total_pages
            })
            
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})



    def _search_logs(self):
        """Search paginated logs in scan_log."""
        try:
            lot_id = request.args.get('lot_id')
            batch_id = request.args.get('batch_id')
            fpc_id = request.args.get('fpc_id')
            header_id = request.args.get('header_id')
            machine_no = request.args.get('machine_no')
            agv_no = request.args.get('agv_no')
            date   = request.args.get('date')      # YYYY-MM-DD
            result_filter = (request.args.get('result_filter') or 'all').lower()
            page   = int(request.args.get('page', 1))
            offset = (page - 1) * Config.PAGE_SIZE

            filters, params = [], []

            if header_id:
                filters.append("header_id LIKE %s")
                params.append(f"%{header_id}%")

            if machine_no:
                filters.append("machine_no LIKE %s")
                params.append(f"%{machine_no}%")

            if agv_no:
                filters.append("agv_no LIKE %s")
                params.append(f"%{agv_no}%")

            if lot_id:
                filters.append("lot_id LIKE %s")
                params.append(f"%{lot_id}%")

            if batch_id:
                filters.append("batch_id LIKE %s")
                params.append(f"%{batch_id}%")

            if fpc_id:
                filters.append("fpc_id LIKE %s")
                params.append(f"%{fpc_id}%")

            if date:
                filters.append("DATE(timestamp) = %s")
                params.append(date)

            if result_filter == 'mismatch':
                filters.append("(UPPER(source) IN ('MISMATCH', 'MISMATCH_DETECTED', 'NOT_ALLOWED'))")
            elif result_filter == 'not_found':
                filters.append("(UPPER(source) = 'NOT_FOUND')")
            elif result_filter == 'match':
                filters.append("(UPPER(source) NOT IN ('MISMATCH', 'MISMATCH_DETECTED', 'NOT_ALLOWED', 'NOT_FOUND') OR source IS NULL)")

            where = ("WHERE " + " AND ".join(filters)) if filters else ""

            conn = DatabaseManager.get_connection()
            cur = conn.cursor()

            cur.execute(f"SELECT COUNT(*) FROM scan_log {where}", tuple(params))
            total = cur.fetchone()[0]
            total_pages = (total + Config.PAGE_SIZE - 1) // Config.PAGE_SIZE if total > 0 else 1

            cur.execute(f"""
                SELECT id, fpc_id, header_id, header_name, timestamp, agv_no, machine_no, batch_id, lot_id, source, touchdown, comment
                FROM scan_log
                {where}
                ORDER BY timestamp DESC
                LIMIT %s OFFSET %s
            """, tuple(params + [Config.PAGE_SIZE, offset]))
            rows = cur.fetchall()
            conn.close()

            logs = []
            for r in rows:
                f_id = r[1]
                h_id = r[2]
                source_val = str(r[9] or '').upper().strip()
                is_nf = (source_val == 'NOT_FOUND')
                is_mis = (source_val in ('MISMATCH', 'MISMATCH_DETECTED', 'NOT_ALLOWED'))
                res_type = 'not_found' if is_nf else ('mismatch' if is_mis else 'match')
                logs.append({
                    "logId":     f"LOG{str(r[0]).zfill(6)}",
                    "fpcId":    f_id,
                    "headerId": h_id,
                    "headerName": r[3],
                    "timestamp": (r[4].strftime('%Y-%m-%d %H:%M:%S') if hasattr(r[4], 'strftime') else str(r[4])) if r[4] else None,
                    "agvNo":    r[5],
                    "machineNo":r[6],
                    "batchId":  r[7],
                    "lotId":    r[8],
                    "source":   source_val,
                    "resultType": res_type,
                    "touchdown": r[10],
                    "comment":  r[11],
                    "isMismatch": is_mis,
                })

            return jsonify({
                "status": "success",
                "logs": logs,
                "total": total,
                "page": page,
                "pages": total_pages
            })

        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    def _get_cassette_logs(self):
        """Get paginated logs from cassette_reader_log table."""
        try:
            page = int(request.args.get('page', 1))
            offset = (page - 1) * Config.PAGE_SIZE

            # --- [NEW] Mockup Mode cassette logs retrieval ---
            # If MOCKUP_MODE is enabled, return simulated log entries instead of querying DB
            if getattr(Config, 'MOCKUP_MODE', False):
                fake_rows = [
                    (2001, 'CASS-001', 'RUNNING', 'LOT-X88', 'BATCH-B01', datetime.now() - timedelta(days=1), datetime.now() + timedelta(days=13), datetime.now() - timedelta(minutes=2), 'AVT#55'),
                    (2002, 'CASS-002', 'CLEANING', 'LOT-Y99', 'BATCH-B02', datetime.now() - timedelta(days=5), datetime.now() + timedelta(days=9), datetime.now() - timedelta(minutes=15), 'AVT#55'),
                    (2003, 'CASS-003', 'RUNNING', 'LOT-Z10', 'BATCH-B03', datetime.now() - timedelta(days=3), datetime.now() + timedelta(days=11), datetime.now() - timedelta(hours=1), 'AVT#55'),
                ]
                total = len(fake_rows)
                total_pages = 1
                logs = [{
                    "logId":          f"CASS{str(r[0]).zfill(6)}",
                    "cassetteId":     r[1],
                    "machineStatus":  r[2],
                    "lotId":          r[3],
                    "batchId":        r[4],
                    "lastCleaning":   r[5].strftime('%Y-%m-%d %H:%M:%S') if r[5] else None,
                    "nextCleaning":   r[6].strftime('%Y-%m-%d %H:%M:%S') if r[6] else None,
                    "timestamp":      r[7].strftime('%Y-%m-%d %H:%M:%S') if r[7] else None,
                    "machineNo":      r[8],
                } for r in fake_rows]
                return jsonify({
                    "status": "success",
                    "logs": logs,
                    "total": total,
                    "page": page,
                    "pages": total_pages
                })

            conn = DatabaseManager.get_connection()
            cur = conn.cursor()

            # Get total rows count
            cur.execute("SELECT COUNT(*) FROM cassette_reader_log")
            total = cur.fetchone()[0]
            total_pages = (total + Config.PAGE_SIZE - 1) // Config.PAGE_SIZE

            # Get paginated rows
            cur.execute("""
                SELECT id, cassette_id, machine_status, lot_id, batch_id, last_cleaning, next_cleaning, timestamp, machine_no
                FROM cassette_reader_log
                ORDER BY timestamp DESC
                LIMIT %s OFFSET %s
            """, (Config.PAGE_SIZE, offset))
            
            rows = cur.fetchall()
            conn.close()

            logs = [{
                "logId":          f"CASS{str(r[0]).zfill(6)}",
                "cassetteId":     r[1],
                "machineStatus":  r[2],
                "lotId":          r[3],
                "batchId":        r[4],
                "lastCleaning":   (r[5].strftime('%Y-%m-%d %H:%M:%S') if hasattr(r[5], 'strftime') else str(r[5])) if r[5] else None,
                "nextCleaning":   (r[6].strftime('%Y-%m-%d %H:%M:%S') if hasattr(r[6], 'strftime') else str(r[6])) if r[6] else None,
                "timestamp":      (r[7].strftime('%Y-%m-%d %H:%M:%S') if hasattr(r[7], 'strftime') else str(r[7])) if r[7] else None,
                "machineNo":      r[8],
            } for r in rows]

            return jsonify({
                "status": "success",
                "logs": logs,
                "total": total,
                "page": page,
                "pages": total_pages
            })
            
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})


    def _search_cassette_logs(self):
        """Search paginated logs in cassette_scan_log."""
        try:
            cassette_id = request.args.get('cassette_id')
            lot_id = request.args.get('lot_id')
            batch_id = request.args.get('batch_id')
            machine_no = request.args.get('machine_no')
            date   = request.args.get('date')      # YYYY-MM-DD
            page   = int(request.args.get('page', 1))
            offset = (page - 1) * Config.PAGE_SIZE

            # --- [NEW] Mockup Mode cassette search filtering ---
            # If MOCKUP_MODE is enabled, simulate SQL queries by filtering python objects
            # to let search inputs work correctly during HMI mockup demo.
            if getattr(Config, 'MOCKUP_MODE', False):
                fake_rows = [
                    (2001, 'CASS-001', 'RUNNING', 'LOT-X88', 'BATCH-B01', datetime.now() - timedelta(days=1), datetime.now() + timedelta(days=13), datetime.now() - timedelta(minutes=2), 'AVT#55'),
                    (2002, 'CASS-002', 'CLEANING', 'LOT-Y99', 'BATCH-B02', datetime.now() - timedelta(days=5), datetime.now() + timedelta(days=9), datetime.now() - timedelta(minutes=15), 'AVT#55'),
                    (2003, 'CASS-003', 'RUNNING', 'LOT-Z10', 'BATCH-B03', datetime.now() - timedelta(days=3), datetime.now() + timedelta(days=11), datetime.now() - timedelta(hours=1), 'AVT#55'),
                ]
                filtered = []
                for r in fake_rows:
                    if cassette_id and cassette_id.lower() not in r[1].lower(): continue
                    if lot_id and lot_id.lower() not in r[3].lower(): continue
                    if batch_id and batch_id.lower() not in r[4].lower(): continue
                    if machine_no and machine_no.lower() not in r[8].lower(): continue
                    if date and r[7].strftime('%Y-%m-%d') != date: continue
                    filtered.append(r)
                
                total = len(filtered)
                total_pages = (total + Config.PAGE_SIZE - 1) // Config.PAGE_SIZE
                rows = filtered[offset : offset + Config.PAGE_SIZE]
                logs = [{
                    "logId":          f"CASS{str(r[0]).zfill(6)}",
                    "cassetteId":     r[1],
                    "machineStatus":  r[2],
                    "lotId":          r[3],
                    "batchId":        r[4],
                    "lastCleaning":   r[5].strftime('%Y-%m-%d %H:%M:%S') if r[5] else None,
                    "nextCleaning":   r[6].strftime('%Y-%m-%d %H:%M:%S') if r[6] else None,
                    "timestamp":      r[7].strftime('%Y-%m-%d %H:%M:%S') if r[7] else None,
                    "machineNo":      r[8],
                } for r in rows]
                return jsonify({
                    "status": "success",
                    "logs": logs,
                    "total": total,
                    "page": page,
                    "pages": total_pages
                })

            filters, params = [], []

            if cassette_id:
                filters.append("cassette_id LIKE %s")
                params.append(f"%{cassette_id}%")

            if lot_id:
                filters.append("lot_id LIKE %s")
                params.append(f"%{lot_id}%")

            if batch_id:
                filters.append("batch_id LIKE %s")
                params.append(f"%{batch_id}%")

            if machine_no:
                filters.append("machine_no LIKE %s")
                params.append(f"%{machine_no}%")

            if date:
                filters.append("DATE(timestamp) = %s")
                params.append(date)

            where = ("WHERE " + " AND ".join(filters)) if filters else ""

            conn = DatabaseManager.get_connection()
            cur = conn.cursor()

            cur.execute(f"SELECT COUNT(*) FROM cassette_reader_log {where}", tuple(params))
            total = cur.fetchone()[0]
            total_pages = (total + Config.PAGE_SIZE - 1) // Config.PAGE_SIZE

            cur.execute(f"""
                SELECT id, cassette_id, machine_status, lot_id, batch_id, last_cleaning, next_cleaning, timestamp, machine_no
                FROM cassette_reader_log
                {where}
                ORDER BY timestamp DESC
                LIMIT %s OFFSET %s
            """, tuple(params + [Config.PAGE_SIZE, offset]))
            rows = cur.fetchall()
            conn.close()

            logs = [{
                "logId":          f"CASS{str(r[0]).zfill(6)}",
                "cassetteId":     r[1],
                "machineStatus":  r[2],
                "lotId":          r[3],
                "batchId":        r[4],
                "lastCleaning":   (r[5].strftime('%Y-%m-%d %H:%M:%S') if hasattr(r[5], 'strftime') else str(r[5])) if r[5] else None,
                "nextCleaning":   (r[6].strftime('%Y-%m-%d %H:%M:%S') if hasattr(r[6], 'strftime') else str(r[6])) if r[6] else None,
                "timestamp":      (r[7].strftime('%Y-%m-%d %H:%M:%S') if hasattr(r[7], 'strftime') else str(r[7])) if r[7] else None,
                "machineNo":      r[8],
            } for r in rows]

            return jsonify({
                "status": "success",
                "logs": logs,
                "total": total,
                "page": page,
                "pages": total_pages
            })
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})


    def _get_system_info(self):
        """Get system information endpoint"""
        try:
            uptime = datetime.now() - self.start_time
            is_mysql = getattr(DatabaseManager, '_MYSQL_ONLINE', False)
            conn_info = f"MariaDB ({Config.DB_CONFIG['host']}:{Config.DB_CONFIG['port']})" if is_mysql else "SQLite (RFID_database_SQLite.db)"
            
            try:
                hostname = socket.gethostname()
                local_ip = socket.gethostbyname(hostname)
            except Exception:
                local_ip = "127.0.0.1"

            log_count_today = 0
            db_status = "Online"
            try:
                conn = DatabaseManager.get_connection()
                cursor = conn.cursor()
                today_str = datetime.now().strftime('%Y-%m-%d')
                cursor.execute("SELECT COUNT(*) FROM scan_log WHERE DATE(timestamp) = %s", (today_str,))
                row = cursor.fetchone()
                log_count_today = row[0] if row else 0
                conn.close()
            except Exception as dbe:
                db_status = "Error"
                print(f"[WARN] _get_system_info DB check: {dbe}")
            
            latest_backup = "-"
            backup_files = sorted(glob.glob("logs/*.csv"), reverse=True)
            if backup_files:
                latest_backup = os.path.basename(backup_files[0])

            # Reader 1: Header (COM4)
            hdr = getattr(self, 'header_reader', None)
            hdr_port = getattr(hdr, 'port', None) or getattr(Config, 'RFID_PORT', 'COM4')
            hdr_baud = getattr(hdr, 'baudrate', None) or getattr(Config, 'RFID_BAUDRATE', 115200)
            hdr_conn = bool(hdr and hdr.is_hw_connected())

            # Reader 2: FPC (COM5)
            fpc = getattr(self, 'fpc_reader', None)
            fpc_port = getattr(fpc, 'port', None) or getattr(Config, 'RFID_PORT_FPC', 'COM5')
            fpc_baud = getattr(fpc, 'baudrate', None) or getattr(Config, 'RFID_BAUDRATE_FPC', 115200)
            fpc_conn = bool(fpc and fpc.is_hw_connected())

            # Reader 3: Cassette (5127 CK USB HID / SmartCard / Serial)
            cass_conn = is_cassette_hw_connected()

            return jsonify({
                'status': 'success',
                'header_reader': {
                    'model': "YRM100 UHF RFID Reader",
                    'port': str(hdr_port),
                    'baudrate': str(hdr_baud),
                    'connected': hdr_conn
                },
                'fpc_reader': {
                    'model': "YRM100 UHF RFID Reader",
                    'port': str(fpc_port),
                    'baudrate': str(fpc_baud),
                    'connected': fpc_conn
                },
                'cassette_reader': {
                    'model': "HID OMNIKEY 5127CK Mini",
                    'port': "USB HID / Wedge",
                    'baudrate': "N/A",
                    'connected': cass_conn
                },
                'model': "YRM100 UHF / HID OMNIKEY 5127CK Mini (3 Readers)",
                'port': f"{hdr_port}, {fpc_port}, USB HID",
                'baudrate': f"{hdr_baud}",
                'connected': (hdr_conn or fpc_conn or cass_conn),
                'uptime': str(uptime).split('.')[0],
                'database': conn_info,
                'python': platform.python_version(),
                'flask': flask.__version__,
                'os': f"{platform.system()} {platform.release()}",
                'ip': local_ip,
                'prober_ip': getattr(Config, 'PROBER_IP', '192.168.3.100'),
                'log_count': log_count_today,
                'db_status': db_status,
                'last_backup': latest_backup
            })
        except Exception as e:
            return jsonify({'status': 'error', 'message': str(e)})

    def _replicate_log(self):
        """Replicate log endpoint"""
        try:
            logs = request.json.get('logs', [])
            if not logs:
                return jsonify({'status': 'error', 'message': 'No logs provided'}), 400

            conn = DatabaseManager.get_connection()
            cursor = conn.cursor()

            inserted = 0
            skipped = 0

            for log in logs:
                header_id = log.get('Header_id') or log.get('header_id')
                timestamp = log.get('Timestamp') or log.get('timestamp')
                machine_no = log.get('Machine_No') or log.get('machine_no')

                if not header_id or not timestamp or not machine_no:
                    skipped += 1
                    continue

                # Check for duplicates
                cursor.execute('''
                    SELECT COUNT(*) FROM header_reader_log
                    WHERE Timestamp = %s AND Header_id = %s
                ''', (timestamp, header_id))
                exists = cursor.fetchone()[0]

                if exists:
                    skipped += 1
                    continue

                cursor.execute('''
                    INSERT INTO header_reader_log (Header_id, Timestamp, Machine_No)
                    VALUES (%s, %s, %s)
                ''', (header_id, timestamp, machine_no))
                inserted += 1

            conn.commit()
            conn.close()

            return jsonify({
                'status': 'success',
                'inserted': inserted,
                'skipped': skipped
            })

        except Exception as e:
            return jsonify({'status': 'error', 'message': str(e)}), 500
    

        
    def initialize_header_reader(self):
        """Initialize HEADER-only reader (#1)"""
        self.header_reader = RFIDReader()  # uses Config.RFID_PORT
        iomap_value = 1  # if you need to gate with PLC 10, wire it here
        if iomap_value == 1:
            print("[HDR] Starting header reader...")
            if self.header_reader.start_reading():
                print("[HDR] header reader running.")
                BackupManager.schedule_daily_backup()
                return True
            else:
                print("[HDR] header reader failed to start.")
                return False
        else:
            print("[HDR] not in reading zone")
            return False

    def initialize_fpc_reader(self):
        """Initialize FPC-only reader (#2) with sensor gate"""
        self.fpc_reader = FPCReader()  # uses Config.RFID_PORT_FPC
        ok = self.fpc_reader.start()
        print("[FPC] start:", ok)
        return ok

    def initialize_cassette_reader(self):
        """Initialize Cassette RFID reader (#3)"""
        cass_port = getattr(Config, 'RFID_PORT_CASSETTE', None)
        ports = [p.device.upper() for p in list_ports.comports()]
        if cass_port and cass_port.upper() in ports:
            self.cassette_reader = RFIDReader(port=cass_port, reader_mode="CASSETTE")
            print(f"[CASS] Starting cassette serial reader on {cass_port}...")
            if self.cassette_reader.start_reading():
                print("[CASS] cassette reader running.")
                return True
            else:
                print("[CASS] cassette reader failed to start.")
                return False
        else:
            conn = is_cassette_hw_connected()
            print(f"[CASS] Cassette reader in USB HID / SmartCard mode. Connected: {conn}")
            return conn


    def run(self, host='0.0.0.0', port=8002, debug=False):
        """Run the Flask application"""
        self.app.run(host=host, port=port, debug=debug)


# =============================================================================
# MAIN EXECUTION
# =============================================================================
def current_role() -> str:
    return session.get("role", "user")

def admin_required(handler):
    from functools import wraps
    @wraps(handler)
    def _wrap(*args, **kwargs):
        if current_role() != "admin":
            return jsonify({"ok": False, "error": "FORBIDDEN"}), 403
        return handler(*args, **kwargs)
    return _wrap

def main():
    """Main application entry point"""
    app = RFIDApp()
    
    # Initialize RFID reader
    app.initialize_header_reader()
    app.initialize_fpc_reader()
    app.initialize_cassette_reader()


    threading.Thread(target=app._reader_watchdog, daemon=True).start()
    threading.Thread(target=sync_loop, daemon=True).start()
    threading.Thread(target=app._both_logger_loop, daemon=True).start()


    # Run the Flask application
    app.run(host='0.0.0.0', port=8002, debug=False)


if __name__ == '__main__':
    main()