"""Open and migrate the managed SQLite database."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from agent_alfred import schema


def open_database(state_dir: Path) -> sqlite3.Connection:
    state_dir.mkdir(mode=0o700, exist_ok=True)
    state_dir.chmod(0o700)
    path = state_dir / "db.sqlite3"
    conn = sqlite3.connect(str(path), check_same_thread=False)
    try:
        path.chmod(0o600)
        schema.migrate(conn)
    except Exception:
        conn.close()
        raise
    return conn
