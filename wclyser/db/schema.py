"""Database connection and forward-only migrations.

Migrations are numbered SQL files in ``migrations/``, applied in order inside a
transaction. There is no down migration: the raw blobs are kept, so a breaking
change is handled by bumping the version, dropping what it affects and
re-importing from cache at no API cost.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..errors import WCLError

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_NAME = re.compile(r"^(\d{3})_(.+)\.sql$")


def connect(db_path: Path, read_only: bool = False) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if read_only:
        if not db_path.is_file():
            raise WCLError(f"no database at {db_path}; run 'wcly import' first")
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def _applied(conn: sqlite3.Connection) -> set[int]:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    return {row["version"] for row in conn.execute("SELECT version FROM schema_version")}


def available_migrations() -> list[tuple[int, str, Path]]:
    found = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = _MIGRATION_NAME.match(path.name)
        if not match:
            raise WCLError(f"migration filename not in NNN_name.sql form: {path.name}")
        found.append((int(match.group(1)), match.group(2), path))
    return found


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply every migration not yet recorded. Returns the versions applied."""
    applied = _applied(conn)
    run: list[int] = []
    for version, name, path in available_migrations():
        if version in applied:
            continue
        log.info("applying migration %03d %s", version, name)
        sql = path.read_text(encoding="utf-8")
        with conn:
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
        run.append(version)
    return run


def open_database(db_path: Path, read_only: bool = False) -> sqlite3.Connection:
    conn = connect(db_path, read_only=read_only)
    if not read_only:
        migrate(conn)
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return row["v"] or 0
