"""SQLite user store."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

CREATE_USERS_SQL = """
CREATE TABLE IF NOT EXISTS users (
  username TEXT PRIMARY KEY,
  hashed_password TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
"""


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        con.execute(CREATE_USERS_SQL)  # idempotent, so no separate migration step
        yield con
        con.commit()
    finally:
        con.close()


def create_user(path: Path, username: str, hashed_password: str) -> bool:
    """Insert a user. Returns False if the username is already taken."""
    with _connect(path) as con:
        try:
            con.execute(
                "INSERT INTO users (username, hashed_password, created_at) VALUES (?, ?, ?)",
                (username, hashed_password, int(time.time())),
            )
        except sqlite3.IntegrityError:
            return False
    return True


def get_user(path: Path, username: str) -> sqlite3.Row | None:
    with _connect(path) as con:
        row: sqlite3.Row | None = con.execute(
            "SELECT username, hashed_password, created_at FROM users WHERE username=?", (username,)
        ).fetchone()
        return row
