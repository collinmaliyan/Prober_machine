import base64
from requests.auth import HTTPBasicAuth

# General HTTP timeout used by MiR requests, etc.
HTTP_TIMEOUT = (2, 5)

# Robots shown in both files
ROBOTS = [
    {
        "name": "Robot FPC no.1",
        "base": "http://92.121.79.54/api/v2.0.0",
        "user": "distributor",
        "pass": "62f2f0f1eff10d3152c95f6f0596576e482bb8e44806433f4cf929792834b014",
        "color": "#00e0ff",
    },
    {
        "name": "Robot FPC no.2",
        "base": "http://92.121.77.8/api/v2.0.0",
        "user": "distributor",
        "pass": "62f2f0f1eff10d3152c95f6f0596576e482bb8e44806433f4cf929792834b014",
        "color": "#ff6b6b",
    },
    {
        "name": "Robot Cassette no.3",
        "base": "http://92.121.77.10/api/v2.0.0",
        "user": "distributor",
        "pass": "62f2f0f1eff10d3152c95f6f0596576e482bb8e44806433f4cf929792834b014",
        "color": "#22c55e",
    },
]

def _headers(user, password):
    """Basic auth header helper used by MiR/robot calls."""
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

class Config:
    """Central app configuration shared by both scripts."""
    # --- [NEW] Mockup Mode Toggle ---
    # Set this to True to enable mockup/presentation mode (simulated data & AGVs).
    # Set this to False for actual production hardware/database connection mode.
    MOCKUP_MODE = False

    DB_CONFIG = {
        'host': '92.121.77.57',
        'port': 3306,
        'user': '13991628',
        'password': '7618HOkrJ',
        'database': 'prober_machine'
    }

    # ——— App / device identity ———
    AGV_NO = "-"
    # --- [COMMENTED OUT] Original machine name ---
    # MACHINE_NO = "AVT#55"
    # --- [NEW] Dynamic machine name variable (updatable via settings API) ---
    MACHINE_NO = "AVT55_TSK54"
    PROBER_IP = "192.168.3.100"

    # ——— Reader behavior ———
    READER_MODE = "HEADER"
    READER_TYPE = "OLD"
    YRM100_GAP_S = 1.0  # seconds between YRM polls
    TAG_TIMEOUT = 8     # seconds to clear last tag
    READ_INTERVAL = 0.02

    # ——— Main server endpoint ———
    MAIN_SERVER_URL = "http://92.121.78.12:8000"

    # ——— MiR Robot API ———
    MIR_URL = "http://92.121.79.23/api/v2.0.0"
    USERNAME = "distributor"
    PASSWORD = "62f2f0f1eff10d3152c95f6f0596576e482bb8e44806433f4cf929792834b014"
    HEADERS = {"Accept-Language": "en"}
    AUTH = HTTPBasicAuth(USERNAME, PASSWORD)

    # ——— Auth / UI ———
    ADMIN_IDS = ["ADMIN"]
    PAGE_SIZE = 15
    # present in Second_header_wo_error.py only; safe to keep here
    SUPPRESS_UI_WARNINGS = True

    # ——— Serial ports ———
    # Windows defaults; change to /dev/ttyUSB* on Linux if needed
    RFID_PORT = "COM4"
    RFID_PORT_FPC = "COM6"
    RFID_PORT_CASSETTE = "COM8"
    RFID_BAUDRATE = 115200

    # ——— Stage Machine Timers / Windows ———
    FPC_WINDOW_S = 10.0              # 10s reading window when sensor goes ACTIVE
    CASSETTE_IN_PROCESS_TIMEOUT_S = 120
    CASSETTE_CLEAR_TIMEOUT_S = 60

    # ——— Laptop simulator (dev/test) ———
    SIMULATE_SENSOR_WITH_KEYBOARD = False
    SENSOR_TOGGLE_KEY = "t"


