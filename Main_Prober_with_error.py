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
    def __init__(self, sqlite_cursor):
        self.cursor = sqlite_cursor

    def execute(self, sql, params=()):
        sql_converted = sql.replace("%s", "?")
        if params is None: params = ()
        return self.cursor.execute(sql_converted, params)

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    def close(self):
        self.cursor.close()

class SQLiteConnWrapper:
    def __init__(self, sqlite_conn):
        self.conn = sqlite_conn

    def cursor(self):
        return SQLiteCursorWrapper(self.conn.cursor())

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
        if DatabaseManager._MYSQL_ONLINE is not None and (now - DatabaseManager._LAST_MYSQL_CHECK < 5.0):
            return DatabaseManager._MYSQL_ONLINE

        host = Config.DB_CONFIG.get('host', 'localhost')
        port = int(Config.DB_CONFIG.get('port', 3306))
        try:
            s = socket.create_connection((host, port), timeout=0.05)
            s.close()
            DatabaseManager._MYSQL_ONLINE = True
        except Exception:
            DatabaseManager._MYSQL_ONLINE = False
            
        DatabaseManager._LAST_MYSQL_CHECK = now
        return DatabaseManager._MYSQL_ONLINE

    @staticmethod
    def get_connection():
        """Get database connection (Instant socket check for MariaDB, zero-delay SQLite fallback)"""
        if DatabaseManager.is_mysql_available():
            try:
                return mysql.connector.connect(**Config.DB_CONFIG)
            except Exception:
                DatabaseManager._MYSQL_ONLINE = False

        import sqlite3
        for db_name in ["RFID_database_SQLite.db", "rfid_proj.db"]:
            db_path = os.path.join(os.path.dirname(__file__), db_name)
            if os.path.exists(db_path):
                return SQLiteConnWrapper(sqlite3.connect(db_path))
        raise Exception("No valid database connection available")
    
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
            # (touchdown BIGINT, latest_pm DATETIME, comment VARCHAR)
            td, pm, cmt = row
            return {
                "touchdown": td,
                "pm_date": pm.strftime('%Y-%m-%d') if pm else None,
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
                    batch_id=None, lot_id=None):
        """Write one immutable snapshot row used by the Log page."""
        try:
            conn = DatabaseManager.get_connection()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO scan_log
                    (timestamp, machine_no, agv_no, fpc_id, header_id, batch_id, lot_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (timestamp, machine_no, agv_no, fpc_id, header_id, batch_id, lot_id))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"[ERROR] store_scan_log: {e}")
            return False

    @staticmethod
    def store_scan_log(timestamp, machine_no, agv_no, fpc_id,
                    header_id=None, header_name=None,
                    batch_id=None, lot_id=None, source='HDR'):
        """Write one immutable snapshot row used by the Log page."""
        try:
            conn = DatabaseManager.get_connection()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO scan_log
                    (source, header_id, header_name, fpc_id,
                    batch_id, lot_id, timestamp, machine_no, agv_no, synced)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (source, header_id, header_name, fpc_id, batch_id, lot_id, 
                timestamp, machine_no, agv_no, 0))  # synced=0 initially
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"[ERROR] store_scan_log: {e}")
            return False

    @staticmethod
    def is_active_pair(header_id: str, fpc_id: str) -> tuple[bool, bool]:
        """
        Returns (active_match, allowed_pair)
        active_match: header.fpc_id == fpc_id
        allowed_pair: exists in fpc_header_allowed (for UX: allowed but not active)
        """
        try:
            conn = DatabaseManager.get_connection()
            cur = conn.cursor(dictionary=True)

            # Is it the active match right now?
            cur.execute("SELECT fpc_id FROM header WHERE header_id=%s LIMIT 1", (header_id,))
            row = cur.fetchone()
            active_match = bool(row and row.get('fpc_id') == fpc_id)

            # Is it at least an allowed pair?
            cur.execute("SELECT 1 AS ok FROM fpc_header_allowed WHERE fpc_id=%s AND header_id=%s LIMIT 1", (fpc_id, header_id))
            row2 = cur.fetchone()
            allowed_pair = bool(row2 and row2.get('ok') == 1)

            cur.close()
            conn.close()
            return active_match, allowed_pair
        except Exception:
            return False, False


    @staticmethod
    def get_enrichment_for_fpc(fpc_id: str) -> tuple[dict | None, dict | None]:
        """
        Convenience wrapper that returns (batch_lot, summary):
        batch_lot: {'batch_id', 'lot_id'}   from `batch`
        summary  : {'touchdown','pm_date','comment'}  from `fpc`
        """
        bl = None
        summ = None
        try:
            bl = DatabaseManager.get_batch_info_by_fpc(fpc_id)  # you already have this
        except Exception:
            pass
        try:
            summ = DatabaseManager.get_fpc_summary(fpc_id)      # you already have this
        except Exception:
            pass
        return bl, summ

    @staticmethod
    def is_pair_allowed(fpc_id, header_id) -> bool:
        try:
            conn = DatabaseManager.get_connection()
            cur = conn.cursor()
            cur.execute("""
                SELECT 1 FROM fpc_header_allowed
                WHERE fpc_id=%s AND header_id=%s LIMIT 1
            """, (fpc_id, header_id))
            ok = cur.fetchone() is not None
            conn.close()
            return ok
        except Exception:
            return False

    @staticmethod
    def is_header_active_for_fpc(header_id, fpc_id) -> bool:
        try:
            conn = DatabaseManager.get_connection()
            cur = conn.cursor()
            cur.execute("SELECT fpc_id FROM header WHERE header_id=%s", (header_id,))
            row = cur.fetchone()
            conn.close()
            if not row or row[0] is None:
                return False
            return str(row[0]) == str(fpc_id)
        except Exception:
            return False

    @staticmethod
    def get_cassette_details(cassette_id):
        """Get cassette details from cassette table"""
        try:
            conn = DatabaseManager.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT machine_status, lot_id, batch_id, last_cleaning, next_cleaning
                FROM cassette
                WHERE cassette_id = %s
            """, (cassette_id,))
            row = cursor.fetchone()
            conn.close()
            if row:
                return {
                    "cassette_id": cassette_id,
                    "machine_status": row[0],
                    "lot_id": row[1],
                    "batch_id": row[2],
                    "last_cleaning": row[3].strftime('%Y-%m-%d %H:%M:%S') if row[3] else None,
                    "next_cleaning": row[4].strftime('%Y-%m-%d %H:%M:%S') if row[4] else None
                }
            # Fallback if tag is not found in database (for testing)
            return {
                "cassette_id": cassette_id,
                "machine_status": "Active",
                "lot_id": f"LOT-{cassette_id}",
                "batch_id": f"BATCH-{cassette_id}",
                "last_cleaning": "2025-10-01 08:00:00",
                "next_cleaning": "2025-10-15 08:00:00"
            }
        except Exception as e:
            print(f"[ERROR] get_cassette_details: {e}")
            # Fallback if database is offline (for simulation/testing)
            if cassette_id:
                return {
                    "cassette_id": cassette_id,
                    "machine_status": "Active",
                    "lot_id": f"LOT-{cassette_id}",
                    "batch_id": f"BATCH-{cassette_id}",
                    "last_cleaning": "2025-10-01 08:00:00",
                    "next_cleaning": "2025-10-15 08:00:00"
                }
            return None

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
        {"Header_id": r[1], "Machine_No": r[2], "Timestamp": r[3].strftime('%Y-%m-%d %H:%M:%S')}
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

def set_q(ser, q_val):
    if not (0 <= q_val <= 15):
        raise ValueError("Q must be 0–15")
    cur = get_query_params(ser)
    if not cur: return False
    msb, lsb = cur
    lsb = (lsb & 0b10000111) | ((q_val & 0x0F) << 3)
    payload = bytes([msb, lsb])
    body = bytes([0x00, 0x0E, 0x00, 0x02]) + payload
    cs = sum(body) & 0xFF
    frame = bytes([0xBB]) + body + bytes([cs, 0x7E])
    ser.write(frame)
    fr = read_frame(ser, timeout_s=0.6)
    return bool(fr and fr[0] == 0x01 and fr[1] == 0x0E and fr[2] == b"\x00")

def try_read_epc(ser, attempts=3):
    for _ in range(attempts):
        ser.write(CMD_SINGLE)
        t_end = time.time() + 0.15
        while time.time() < t_end:
            fr = read_frame(ser, timeout_s=0.05)
            if not fr: continue
            ftype, cmd, payload = fr
            if ftype == 0x02 and cmd == 0x22 and len(payload) >= 5:
                epc_len = len(payload) - 5
                if epc_len > 0:
                    return payload[3:3+epc_len].hex().upper()
            elif ftype == 0x01 and cmd == 0xFF and payload == b"\x15":
                break

def yrm_read_frame(ser, timeout_s=0.25):
    """Return (type, cmd, payload) or None."""
    import time
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
# Sensor helper: supports GPIO (Pi) or MiR register polling
# -----------------------------------------------------------------
def _safe_import_gpio():
    try:
        import RPi.GPIO as GPIO
        return GPIO
    except Exception:
        return None

class SensorGate:
    def __init__(self):
        self.mode = getattr(Config, 'SENSOR_MODE', 'GPIO').upper()
        self.active_high = bool(getattr(Config, 'SENSOR_ACTIVE_HIGH', True))
        self.GPIO = None
        self._setup_done = False
        # --- keyboard simulator state ---
        self._simulate = bool(getattr(Config, 'SIMULATE_SENSOR_WITH_KEYBOARD', False))
        self._sim_state = False  # start as INACTIVE
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
                print(f"[SENSOR] Keyboard simulation ON — press '{self._sim_key.upper()}' to toggle ACTIVE/INACTIVE")
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
                        print(f"[SENSOR] keyboard toggle → {'ACTIVE' if self._sim_state else 'INACTIVE'}")
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
                            print(f"[SENSOR] keyboard toggle → {'ACTIVE' if self._sim_state else 'INACTIVE'}")
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def is_active(self) -> bool:
        # Simulator takes precedence (for laptop testing)
        if getattr(self, '_simulate', False):
            return self._sim_state

        # Real sensor paths (GPIO or MiR)
        try:
            if not self._setup_done:
                return False
            if self.mode == 'GPIO':
                if self.GPIO is None:
                    return False
                val = self.GPIO.input(Config.SENSOR_PIN)
                return (val == 1) if self.active_high else (val == 0)
            elif self.mode == 'MIR':
                v = MiRAPI.get_plc_register(getattr(Config, 'SENSOR_MIR_REGISTER', 82))
                try:
                    iv = int(v)
                except Exception:
                    return False
                return (iv == 1) if self.active_high else (iv == 0)
            return False
        except Exception as e:
            print("[SENSOR] read error:", e)
            return False

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
                self.ser = serial.Serial(self.port, self.baudrate, timeout=1)
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
        """Return True if the configured COM port is present."""
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
        print("[LISTENING] YRM100…")
        while self.running:
            try:
                now = time.time()
                epc_ascii = self.single_epc_ascii()
                if epc_ascii:
                    if epc_ascii != self.last_tag or self.last_tag is None:
                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        mode = self.reader_mode if self.reader_mode else getattr(Config, "READER_MODE", "HEADER").upper()
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
            # (Reserved) FPC READER (R#2) — optional
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
        self.window_committed = False   # <-- NEW: one-and-done per window
        self.block_until_low = False
           # when we closed the window

        self.current_data = {
            "fpc_id": None,
            "timestamp": None,
        }

    def connect(self):
        try:
            if self.ser is None or not self.ser.is_open:
                self.ser = serial.Serial(self.port, self.baudrate, timeout=1)
                print(f"[FPC] connected {self.port}")
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

        # If we’re clearing due to sensor LOW while a tag was held,
        # raise a one-shot flag only if we haven't committed this window yet.
        if ("sensor LOW" in reason) and had_tag and (not self.window_committed):
            self.window_just_closed = True
            self.last_window_fpc_id = last
            self.last_window_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.window_committed = True
            if getattr(Config, 'BOTH_VERBOSE', False):
                print(f"[FPC] window closed flag set (sensor LOW): fpc={last} @ {self.last_window_timestamp}")

        if self.fpc_current and getattr(Config, 'BOTH_VERBOSE', False):
            print(f"[FPC] CLEAR ({reason}) fpc_id={self.fpc_current}")

        # Reset live state; next window will reset window_committed back to False on open
        self.fpc_current = None
        self.current_data.update({"fpc_id": None, "timestamp": None})
        self.fpc_logged_latch = None
        self.window_open = False
        self.window_until = 0.0



    def _loop(self):
        print("[FPC] loop starting…")
        gap = getattr(Config, "YRM100_GAP_S", 1.0)
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
                    print(f"[FPC] sensor ACTIVE → open window {getattr(Config, 'FPC_WINDOW_S', 10.0)}s")


                # if window open, try to read
                if self.window_open:
                    if not active:
                        # sensor dropped → clear immediately
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
                                    # one-shot store to fpc_reader_log (per window)
                                    if self.fpc_logged_latch != epc_ascii:
                                        DatabaseManager.store_fpc_log(epc_ascii, ts)
                                        self.fpc_logged_latch = epc_ascii
                                # keep holding while sensor is active
                        else:
                            # window expired:
                            if not self.fpc_current:
                                # no read within window → clear & block re-open until sensor LOW
                                self._clear("window timeout (no read)")
                                self.block_until_low = True
                                if getattr(Config, 'BOTH_VERBOSE', False):
                                    print("[FPC] window timeout (no read) → BLANK and BLOCK until sensor LOW")
                            else:
                                # we DID read a tag during the window → raise one-shot flag for coordinator
                                if not self.window_committed and not self.window_just_closed:
                                    self.window_just_closed = True
                                    self.last_window_fpc_id = self.fpc_current
                                    self.last_window_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                    self.window_committed = True                   # <-- NEW (prevent future commits)
                                    if getattr(Config, 'BOTH_VERBOSE', False):
                                        print(f"[FPC] window timeout → commit flag set for fpc={self.fpc_current} @ {self.last_window_timestamp}")
                                # keep holding value on screen; clear will happen when sensor goes LOW

                else:
                    # no window
                    if not active:
                        # sensor LOW → always unblock for the next rising edge
                        if self.block_until_low:
                            self.block_until_low = False
                            if getattr(Config, 'BOTH_VERBOSE', False):
                                print("[FPC] sensor LOW → UNBLOCKED; next ACTIVE will open a new window")
                        # ensure cleared if anything is still shown
                        if self.fpc_current:
                            self._clear("sensor LOW (idle)")

                time.sleep(gap)
            except Exception as e:
                print("[FPC] loop error:", e)
                time.sleep(0.3)

    def snapshot(self):
        # small helper for coordinator/UI
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
                # r[6] is the timestamp column from SELECT → format as text
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
        self._hdr_seen_timestamp = None  # ADD THIS LINE
        self._header_for_fpc = None
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
            "timestamp": None
        }

        # --- [NEW] Mockup Mode variables initialization ---
        # These variables store the simulated states for FPC and Cassette during mockup demo
        self._live_mock_fpc = {}
        self._live_mock_cassette = {}
        if getattr(Config, 'MOCKUP_MODE', False):
            print("[MOCK] Mockup Mode is enabled. Starting mockup simulation loop...")
            # Start background thread to simulate RFID tag readings
            t = threading.Thread(target=self._mockup_simulator_loop, daemon=True)
            t.start()

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

            # --- connectivity: use header_reader as the “reader connected” indicator ---
            is_connected = False
            try:
                if getattr(self, 'header_reader', None):
                    is_connected = bool(self.header_reader.is_hw_connected())
                    if is_connected and not self.header_reader.running:
                        print("[AUTO] Header reader detected, starting thread…")
                        self.header_reader.start_reading()
                    elif not is_connected and self.header_reader.running:
                        self.header_reader.running = False
            except Exception as e:
                print(f"[WARN] header is_hw_connected failed: {e}")
                is_connected = False

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


            # --- merge HEADER live state ---
            try:
                if getattr(self, 'header_reader', None):
                    hdr = self.header_reader.get_current_data()
                    if isinstance(hdr, dict):
                        # Show header_id immediately
                        if hdr.get('header_id') is not None:
                            current['header_id'] = hdr.get('header_id')
                            
                            # Start delay timer when header is first detected
                            if self._hdr_seen_timestamp is None or self._hdr_seen_in_window != hdr.get('header_id'):
                                self._hdr_seen_in_window = hdr.get('header_id')
                                self._hdr_seen_timestamp = time.time()
                                self._header_for_fpc = None  # Reset FPC value
                            
                            # After delay, copy header to FPC field
                            header_delay = float(getattr(Config, 'HEADER_TO_FPC_DELAY', 2.0))
                            elapsed = time.time() - self._hdr_seen_timestamp
                            if elapsed >= header_delay:
                                self._header_for_fpc = self._hdr_seen_in_window
                            
                        if hdr.get('header_name') is not None:
                            current['header_name'] = hdr.get('header_name')
                        if hdr.get('timestamp'):
                            current['timestamp'] = hdr.get('timestamp')
            except Exception as e:
                print("[WARN] merge HEADER snapshot failed:", e)
            # --- merge FPC live state (or use delayed header) ---
            try:
                # If we have a delayed header value, use it as FPC
                if self._header_for_fpc:
                    current['fpc_id'] = self._header_for_fpc
                
                # Otherwise check if there's a real FPC reader
                elif getattr(self, 'fpc_reader', None):
                    f = self.fpc_reader.snapshot()
                    if isinstance(f, dict) and f.get('fpc_id'):
                        current['fpc_id'] = f['fpc_id']
                        # prefer a timestamp if we don't have one yet
                        if not current['timestamp'] and f.get('timestamp'):
                            current['timestamp'] = f['timestamp']
            except Exception as e:
                print("[WARN] merge FPC snapshot failed:", e)

            # --- merge FPC live state ---
            try:
                if getattr(self, 'fpc_reader', None):
                    f = self.fpc_reader.snapshot()
                    if isinstance(f, dict) and f.get('fpc_id'):
                        current['fpc_id'] = f['fpc_id']
                        # prefer a timestamp if we don't have one yet
                        if not current['timestamp'] and f.get('timestamp'):
                            current['timestamp'] = f['timestamp']
            except Exception as e:
                print("[WARN] merge FPC snapshot failed:", e)
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
            if hdr and fpc:
                allowed = DatabaseManager.is_pair_allowed(fpc, hdr)
                active_pair = DatabaseManager.is_header_active_for_fpc(hdr, fpc)
                current['allowed'] = allowed
                current['active'] = active_pair
                current['active_pair'] = active_pair  # alias for convenience
                current['match_ok'] = bool(allowed and active_pair)
                if not current['match_ok']:
                    if allowed is False:
                        current['mismatch_reason'] = 'FPC/Header not allowed together'
                    elif active_pair is False:
                        current['mismatch_reason'] = 'Header active on different FPC'

            # --- Check cassette reader hardware state ---
            is_cassette_connected = False
            try:
                if getattr(self, 'cassette_reader', None):
                    is_cassette_connected = bool(self.cassette_reader.is_hw_connected())
                    if is_cassette_connected and not self.cassette_reader.running:
                        print("[AUTO] Cassette reader detected, starting thread…")
                        self.cassette_reader.start_reading()
                    elif not is_cassette_connected and self.cassette_reader.running:
                        self.cassette_reader.running = False
            except Exception as e:
                print(f"[WARN] cassette is_hw_connected failed: {e}")
                is_cassette_connected = False

            # Merge live cassette state
            try:
                if getattr(self, 'cassette_reader', None):
                    cass = self.cassette_reader.get_current_data()
                    if isinstance(cass, dict) and cass.get('cassette_id'):
                        c_id = cass.get('cassette_id')
                        if self._cassette_state.get('cassette_id') != c_id:
                            details = DatabaseManager.get_cassette_details(c_id)
                            if details:
                                self._cassette_state.update(details)
                                self._cassette_state['timestamp'] = cass.get('timestamp')
                                # Log it to the database
                                DatabaseManager.store_cassette_log(
                                    c_id, 
                                    details.get('machine_status'),
                                    details.get('lot_id'),
                                    details.get('batch_id'),
                                    details.get('last_cleaning'),
                                    details.get('next_cleaning'),
                                    cass.get('timestamp')
                                )
                    elif isinstance(cass, dict) and cass.get('cassette_id') is None:
                        # Clear cassette state if tag cleared (and not simulated)
                        if not getattr(self, '_cassette_simulated', False):
                            for k in self._cassette_state:
                                self._cassette_state[k] = None
            except Exception as e:
                print("[WARN] merge Cassette snapshot failed:", e)

            # --- [COMMENTED OUT] Old non-mockup jsonify block ---
            # return jsonify({
            #     'status': 'success',
            #     'reader_connected': is_connected,
            #     'data': current,
            #     'cassette_connected': is_cassette_connected or getattr(self, '_cassette_simulated_connected', False),
            #     'cassette': self._cassette_state
            # })

            rfid_status = {
                'fpc': {
                    'connected': False,
                    'sensor': 'OFF'
                },
                'cassette': {
                    'connected': False,
                    'sensor': 'ON'
                },
                'header': {
                    'connected': False,
                    'sensor': 'ON'
                }
            }

            if getattr(self, 'fpc_reader', None):
                fpc_conn = bool(self.fpc_reader.ser and self.fpc_reader.ser.is_open)
                fpc_sens = 'ON' if self.fpc_reader.sensor.is_active() else 'OFF'
                rfid_status['fpc']['connected'] = fpc_conn
                rfid_status['fpc']['sensor'] = fpc_sens
            
            if getattr(self, 'header_reader', None):
                hdr_conn = bool(self.header_reader.is_hw_connected())
                rfid_status['header']['connected'] = hdr_conn

            if getattr(self, 'cassette_reader', None):
                rfid_status['cassette']['connected'] = bool(self.cassette_reader.is_hw_connected())

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

                # FPC reader (optional auto-recover)
                if self.fpc_reader:
                    try:
                        # if port disappeared, try to reconnect/start
                        # (we don't have an is_hw_connected() on FPCReader; do a cheap connect())
                        if not self.fpc_reader.running:
                            self.fpc_reader.start()
                    except Exception as e:
                        print("[WATCHDOG] fpc error:", e)

                # Cassette reader (auto-recover)
                if self.cassette_reader:
                    try:
                        present = self.cassette_reader.is_hw_connected()
                        if present and not self.cassette_reader.running:
                            print("[WATCHDOG] USB present; starting CASSETTE reader")
                            self.cassette_reader.start_reading()
                    except Exception as e:
                        print("[WATCHDOG] cassette error:", e)
            except Exception as e:
                print("[WATCHDOG] loop error:", e)

            time.sleep(1)


    def _both_logger_loop(self):
        """
        Commit one scan_log row *after* the FPC reader stops reading:
        - While FPC window is OPEN, remember the latest header seen.
        - When FPC window CLOSES due to sensor LOW *and a tag was held*,
          insert exactly one scan_log using (cached_header, last_window_fpc_id).
        """
        gap = 0.2
        while True:
            try:
                # read current HEADER and FPC states
                hdr_id_now = None
                fpc_window_open = False
                fpc_closed_flag = False
                fpc_last_id = None
                ts_now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

                if self.header_reader and isinstance(self.header_reader.get_current_data(), dict):
                    hdr_id_now = self.header_reader.get_current_data().get('header_id')

                if self.fpc_reader:
                    fpc_window_open = bool(self.fpc_reader.window_open)
                    fpc_closed_flag = bool(self.fpc_reader.window_just_closed)
                    fpc_last_id = self.fpc_reader.last_window_fpc_id

                # While window is open, keep caching the latest header_id
                if fpc_window_open and hdr_id_now:
                    self._hdr_seen_in_window = hdr_id_now

                # If the window just closed (sensor LOW) *and we had a tag*, do one insert
                if fpc_closed_flag and fpc_last_id:
                    hdr_for_commit = self._hdr_seen_in_window or hdr_id_now

                    if hdr_for_commit:
                        # ---- Phase 3: evaluate pair + optional enrichment ----
                        active_match, allowed_pair = DatabaseManager.is_active_pair(hdr_for_commit, fpc_last_id)

                        batch_id = lot_id = None
                        touchdown = pm_date = comment = None

                        if active_match:
                            # Only enrich when active now
                            bl, summ = DatabaseManager.get_enrichment_for_fpc(fpc_last_id)
                            if bl:
                                batch_id = bl.get('batch_id')
                                lot_id   = bl.get('lot_id')
                            if summ:
                                touchdown = summ.get('touchdown')
                                pm_date   = summ.get('pm_date')
                                comment   = summ.get('comment')

                            # cache for UI
                            self._pair_state.update({
                                "pair_ok": True,
                                "pair_status": "active_match",
                                "header_id": hdr_for_commit,
                                "fpc_id": fpc_last_id,
                                "batch_id": batch_id,
                                "lot_id": lot_id,
                                "touchdown": touchdown,
                                "pm_date": pm_date,
                                "comment": comment,
                                "ts": ts_now,
                            })
                        else:
                            status = "allowed_but_inactive" if allowed_pair else "mismatch"
                            # do not enrich on inactive/mismatch; just cache the status
                            self._pair_state.update({
                                "pair_ok": False,
                                "pair_status": status,
                                "header_id": hdr_for_commit,
                                "fpc_id": fpc_last_id,
                                "batch_id": None,
                                "lot_id": None,
                                "touchdown": None,
                                "pm_date": None,
                                "comment": None,
                                "ts": ts_now,
                            })
                        # ---- Detect mismatch and trigger frontend warning ----
                    if not active_match and not allowed_pair:
                        # Store mismatch flag for frontend to pick up
                        self._pair_state.update({
                            "mismatch_detected": True,
                            "mismatch_type": "not_allowed",
                            "mismatch_header": hdr_for_commit,
                            "mismatch_fpc": fpc_last_id
                        })
                        print(f"[MISMATCH] Header {hdr_for_commit} + FPC {fpc_last_id} NOT ALLOWED")
                    elif not active_match and allowed_pair:
                        self._pair_state.update({
                            "mismatch_detected": True,
                            "mismatch_type": "inactive",
                            "mismatch_header": hdr_for_commit,
                            "mismatch_fpc": fpc_last_id
                        })
                        print(f"[MISMATCH] Header {hdr_for_commit} + FPC {fpc_last_id} allowed but INACTIVE")
                    else:
                        # Clear mismatch flags when pair is good
                        self._pair_state.update({
                            "mismatch_detected": False,
                            "mismatch_type": None
                        })
                        # ---- INSERT one-shot to scan_log (same as before) ----
                        try:
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
                                'BOTH', hdr_for_commit, None, fpc_last_id,
                                batch_id, lot_id, touchdown, pm_date, comment,
                                getattr(Config, 'AGV_NO', None),
                                getattr(Config, 'MACHINE_NO', '-'),
                                ts_now
                            ))
                            conn.commit()
                            conn.close()
                            print(f"[BOTH] scan_log inserted (one-shot): header={hdr_for_commit}, fpc={fpc_last_id} | active={active_match} allowed={allowed_pair}")
                        except Exception as e:
                            print("[BOTH] scan_log insert failed:", e)

                            if self.fpc_reader:
                                self.fpc_reader.window_committed = True   # mark window done
                    # reset one-shot state for next window
                    self._hdr_seen_in_window = None
                    if self.fpc_reader:
                        self.fpc_reader.window_just_closed = False
                        self.fpc_reader.last_window_fpc_id = None
                        self.fpc_reader.last_window_timestamp = None

            except Exception as e:
                print("[BOTH] loop error:", e)

            time.sleep(gap)

    def _get_logs(self):
        """Get paginated logs from snapshot table (scan_log)."""
        try:
            page = int(request.args.get('page', 1))
            offset = (page - 1) * Config.PAGE_SIZE

            # --- [NEW] Mockup Mode logs retrieval ---
            # If MOCKUP_MODE is enabled, return simulated log entries instead of querying DB
            if getattr(Config, 'MOCKUP_MODE', False):
                fake_rows = [
                    (1001, 'P15230-FHH-2362', 'H15230-PHS-02', 'Header FPC 02', datetime.now() - timedelta(minutes=2), 'AGV_2', 'AVT#55', 'BATCH-888', 'LOT-999'),
                    (1002, 'P13080-FHB-0364', 'H13080-PHS-11', 'Header FPC 11', datetime.now() - timedelta(minutes=15), 'AGV_1', 'AVT#55', 'BATCH-777', 'LOT-666'),
                    (1003, '2ID031FV002B', 'H15230-PHS-03', 'Header FPC 03', datetime.now() - timedelta(hours=1), 'AGV_2', 'AVT#55', 'BATCH-111', 'LOT-222'),
                    (1004, '2ID018FV002B', 'H13080-PHS-11', 'Header FPC 11', datetime.now() - timedelta(hours=3), 'AGV_1', 'AVT#55', 'BATCH-002', 'LOT-003'),
                    (1005, '2ID031FV001B', 'H15230-PHS-02', 'Header FPC 02', datetime.now() - timedelta(days=1), 'AGV_2', 'AVT#55', 'BATCH-004', 'LOT-005'),
                ]
                total = len(fake_rows)
                total_pages = 1
                logs = [{
                    "logId":     f"LOG{str(r[0]).zfill(6)}",
                    "fpcId":     r[1],
                    "headerId":  r[2],
                    "headerName": r[3],
                    "timestamp": r[4].strftime('%Y-%m-%d %H:%M:%S') if r[4] else None,
                    "agvNo":     r[5],
                    "machineNo": r[6],
                    "batchId":   r[7],
                    "lotId":     r[8],
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
            cur.execute("SELECT COUNT(*) FROM scan_log")
            total = cur.fetchone()[0]
            total_pages = (total + Config.PAGE_SIZE - 1) // Config.PAGE_SIZE

            # Get paginated rows
            cur.execute("""
                SELECT id, fpc_id, header_id, header_name, timestamp, agv_no, machine_no, batch_id, lot_id
                FROM scan_log
                ORDER BY timestamp DESC
                LIMIT %s OFFSET %s
            """, (Config.PAGE_SIZE, offset))
            
            rows = cur.fetchall()
            conn.close()

            logs = [{
                "logId":     f"LOG{str(r[0]).zfill(6)}",
                "fpcId":     r[1],
                "headerId":  r[2],
                "headerName": r[3],
                "timestamp": (r[4].strftime('%Y-%m-%d %H:%M:%S') if hasattr(r[4], 'strftime') else str(r[4])) if r[4] else None,
                "agvNo":     r[5],
                "machineNo": r[6],
                "batchId":   r[7],
                "lotId":     r[8],
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
            page   = int(request.args.get('page', 1))
            offset = (page - 1) * Config.PAGE_SIZE

            # --- [NEW] Mockup Mode search filtering ---
            # If MOCKUP_MODE is enabled, simulate SQL queries by filtering python objects
            # to let search inputs work correctly during HMI mockup demo.
            if getattr(Config, 'MOCKUP_MODE', False):
                fake_rows = [
                    (1001, 'P15230-FHH-2362', 'H15230-PHS-02', 'Header FPC 02', datetime.now() - timedelta(minutes=2), 'AGV_2', 'AVT#55', 'BATCH-888', 'LOT-999'),
                    (1002, 'P13080-FHB-0364', 'H13080-PHS-11', 'Header FPC 11', datetime.now() - timedelta(minutes=15), 'AGV_1', 'AVT#55', 'BATCH-777', 'LOT-666'),
                    (1003, '2ID031FV002B', 'H15230-PHS-03', 'Header FPC 03', datetime.now() - timedelta(hours=1), 'AGV_2', 'AVT#55', 'BATCH-111', 'LOT-222'),
                    (1004, '2ID018FV002B', 'H13080-PHS-11', 'Header FPC 11', datetime.now() - timedelta(hours=3), 'AGV_1', 'AVT#55', 'BATCH-002', 'LOT-003'),
                    (1005, '2ID031FV001B', 'H15230-PHS-02', 'Header FPC 02', datetime.now() - timedelta(days=1), 'AGV_2', 'AVT#55', 'BATCH-004', 'LOT-005'),
                ]
                filtered = []
                for r in fake_rows:
                    if header_id and header_id.lower() not in r[2].lower(): continue
                    if machine_no and machine_no.lower() not in r[6].lower(): continue
                    if agv_no and agv_no.lower() not in r[5].lower(): continue
                    if lot_id and lot_id.lower() not in r[8].lower(): continue
                    if batch_id and batch_id.lower() not in r[7].lower(): continue
                    if fpc_id and fpc_id.lower() not in r[1].lower(): continue
                    if date and r[4].strftime('%Y-%m-%d') != date: continue
                    filtered.append(r)
                
                total = len(filtered)
                total_pages = (total + Config.PAGE_SIZE - 1) // Config.PAGE_SIZE
                rows = filtered[offset : offset + Config.PAGE_SIZE]
                logs = [{
                    "fpcId":    r[1],
                    "headerId": r[2],
                    "timestamp": r[4].strftime('%Y-%m-%d %H:%M:%S') if r[4] else None,
                    "agvNo":    r[5],
                    "machineNo":r[6],
                    "batchId":  r[7],
                    "lotId":    r[8],
                } for r in rows]
                return jsonify({
                    "status": "success",
                    "logs": logs,
                    "total": total,
                    "page": page,
                    "pages": total_pages
                })

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

            where = ("WHERE " + " AND ".join(filters)) if filters else ""

            conn = DatabaseManager.get_connection()
            cur = conn.cursor()

            cur.execute(f"SELECT COUNT(*) FROM scan_log {where}", tuple(params))
            total = cur.fetchone()[0]
            total_pages = (total + Config.PAGE_SIZE - 1) // Config.PAGE_SIZE

            cur.execute(f"""
                SELECT id, fpc_id, header_id, timestamp, agv_no, machine_no, batch_id, lot_id
                FROM scan_log
                {where}
                ORDER BY timestamp DESC
                LIMIT %s OFFSET %s
            """, tuple(params + [Config.PAGE_SIZE, offset]))
            rows = cur.fetchall()
            conn.close()

            logs = [{
                "fpcId":    r[1],
                "headerId": r[2],
                "timestamp": (r[3].strftime('%Y-%m-%d %H:%M:%S') if hasattr(r[3], 'strftime') else str(r[3])) if r[3] else None,
                "agvNo":    r[4],
                "machineNo":r[5],
                "batchId":  r[6],
                "lotId":    r[7],
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

            # Reader 1: Header
            hdr = getattr(self, 'header_reader', None)
            hdr_port = getattr(hdr, 'port', None) or getattr(Config, 'RFID_PORT', '/dev/ttyUSB0')
            hdr_baud = getattr(hdr, 'baudrate', None) or getattr(Config, 'RFID_BAUDRATE', 115200)
            hdr_conn = bool(hdr and getattr(hdr, 'ser', None) and getattr(hdr.ser, 'is_open', False))

            # Reader 2: FPC
            fpc = getattr(self, 'fpc_reader', None)
            fpc_port = getattr(fpc, 'port', None) or getattr(Config, 'RFID_PORT_FPC', '/dev/ttyUSB1')
            fpc_baud = getattr(fpc, 'baudrate', None) or getattr(Config, 'RFID_BAUDRATE_FPC', 115200)
            fpc_conn = bool(fpc and getattr(fpc, 'ser', None) and getattr(fpc.ser, 'is_open', False))

            # Reader 3: Cassette
            cass = getattr(self, 'cassette_reader', None)
            cass_port = getattr(cass, 'port', None) or getattr(Config, 'RFID_PORT_CASSETTE', '/dev/ttyUSB2')
            cass_baud = getattr(cass, 'baudrate', None) or getattr(Config, 'RFID_BAUDRATE_CASSETTE', 9600)
            cass_conn = bool(cass and getattr(cass, 'ser', None) and getattr(cass.ser, 'is_open', False))

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
                    'port': str(cass_port),
                    'baudrate': str(cass_baud),
                    'connected': cass_conn
                },
                'model': "YRM100 UHF / HID OMNIKEY 5127CK Mini (3 Readers)",
                'port': f"{hdr_port}, {fpc_port}, {cass_port}",
                'baudrate': f"{hdr_baud} / {cass_baud}",
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
            print("[HDR] Starting header reader…")
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
        self.cassette_reader = RFIDReader(port=Config.RFID_PORT_CASSETTE, reader_mode="CASSETTE")
        print("[CASS] Starting cassette reader…")
        if self.cassette_reader.start_reading():
            print("[CASS] cassette reader running.")
            return True
        else:
            print("[CASS] cassette reader failed to start.")
            return False


    def run(self, host='0.0.0.0', port=8001, debug=False):
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
    app.run(host='0.0.0.0', port=8001, debug=False)


if __name__ == '__main__':
    main()