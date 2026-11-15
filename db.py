import os
import sqlite3
import threading
from app.plugins.functions import get_plugins_persistent_path

ROOT = get_plugins_persistent_path(os.path.basename(os.path.dirname(os.path.abspath(__file__))))

SCHEMA = """
CREATE TABLE IF NOT EXISTS missions(
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    settings TEXT NOT NULL,
    geometry TEXT NOT NULL,
    plan TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER,
    updated_by INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS missions_project ON missions(project_id);
"""

_lock = threading.Lock()
_ready = False


def connect():
    global _ready
    if not _ready:
        with _lock:
            if not _ready:
                os.makedirs(ROOT, exist_ok=True)
                c = sqlite3.connect(os.path.join(ROOT, "missions.sqlite"), timeout=30)
                c.execute("PRAGMA journal_mode=WAL")
                c.executescript(SCHEMA)
                c.close()
                _ready = True
    c = sqlite3.connect(os.path.join(ROOT, "missions.sqlite"), timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    return c
