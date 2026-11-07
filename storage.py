"""Shared local SQLite storage."""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def database():
    return Path(os.getenv('AGENTHOOD_DB', str(ROOT / 'agenthood.db')))


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect():
    path = database()
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, timeout=15)
    c.row_factory = sqlite3.Row
    try:
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('PRAGMA journal_mode=WAL')
        with c:
            yield c
    finally:
        c.close()
