from __future__ import annotations
import json, time, atexit
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from pathlib import Path
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool
from monitor_settings import SQLITE_PATH, JSONL_PATH

ISO = "%Y-%m-%dT%H:%M:%S.%fZ"

class Event(BaseModel):
    ts: float = Field(default_factory=lambda: time.time())  # epoch seconds
    iso: str  = Field(default_factory=lambda: datetime.now(timezone.utc).strftime(ISO))
    level: str  # INFO | WARN | ERROR
    kind: str   # DATA | SIGNAL | BLOCK | ORDER | FILL | STATE | PNL | HEARTBEAT
    symbol: Optional[str] = None

    # Common fields
    price: Optional[float] = None
    size: Optional[int] = None
    side: Optional[str] = None  # LONG/SHORT/BUY/SELL
    reason: Optional[str] = None  # for BLOCK/ERROR
    details: Optional[Dict[str, Any]] = None  # any JSON extras
    mode: str = "live"  # live | backtest

class Monitor:
    def __init__(self, sqlite_path: Path = SQLITE_PATH, jsonl_path: Path = JSONL_PATH):
        self.sqlite_path = sqlite_path
        self.jsonl_path  = jsonl_path
        self.engine: Engine = create_engine(
            f"sqlite:///{sqlite_path}",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            future=True,
        )
        self._setup_sqlite()
        self._init_db()
        self._jsonl = open(self.jsonl_path, "a", buffering=1, encoding="utf-8")
        atexit.register(self._jsonl.close)

    def _setup_sqlite(self):
        # Enable WAL and set busy timeout to reduce lock errors on concurrent read/write
        with self.engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
            conn.exec_driver_sql("PRAGMA busy_timeout=3000;")
            conn.exec_driver_sql("PRAGMA synchronous=NORMAL;")

    def _init_db(self):
        # IMPORTANT: execute statements separately (SQLite cannot handle multi-statement via .execute)
        with self.engine.begin() as conn:
            conn.exec_driver_sql(
                """                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  ts REAL,
                  iso TEXT,
                  level TEXT,
                  kind TEXT,
                  symbol TEXT,
                  price REAL,
                  size INTEGER,
                  side TEXT,
                  reason TEXT,
                  details TEXT,
                  mode TEXT
                )
                """
            )
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_events_symbol_ts ON events(symbol, ts)")

    def log_event(self, e: Event):
        payload = e.model_dump()
        # JSONL (append)
        self._jsonl.write(json.dumps(payload, ensure_ascii=False) + "\n")
        # SQLite
        with self.engine.begin() as conn:
            conn.exec_driver_sql(
                """                INSERT INTO events (ts, iso, level, kind, symbol, price, size, side, reason, details, mode)
                VALUES (:ts, :iso, :level, :kind, :symbol, :price, :size, :side, :reason, :details, :mode)
                """,
                {
                    "ts": payload["ts"],
                    "iso": payload["iso"],
                    "level": payload["level"],
                    "kind": payload["kind"],
                    "symbol": payload.get("symbol"),
                    "price": payload.get("price"),
                    "size": payload.get("size"),
                    "side": payload.get("side"),
                    "reason": payload.get("reason"),
                    "details": json.dumps(payload.get("details") or {}, ensure_ascii=False),
                    "mode": payload.get("mode", "live"),
                },
            )

# Singleton instance
monitor = Monitor()

# Convenience helpers
def info(kind: str, **kwargs):
    monitor.log_event(Event(level="INFO", kind=kind, **kwargs))

def warn(kind: str, **kwargs):
    monitor.log_event(Event(level="WARN", kind=kind, **kwargs))

def error(kind: str, **kwargs):
    monitor.log_event(Event(level="ERROR", kind=kind, **kwargs))
