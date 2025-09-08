from pathlib import Path

# ---- Storage paths ----
DATA_DIR = Path("./runtime_data").resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

SQLITE_PATH = DATA_DIR / "events.db"
JSONL_PATH  = DATA_DIR / "events.jsonl"

# ---- Dashboard behavior ----
DASH_REFRESH_SECS = 1  # polling interval in seconds
FAST_WINDOW_ROWS = 20000  # default max rows to pull quickly
