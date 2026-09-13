"""SQLite connection handling and migrations.

The database is both the source of truth and the work queue (PLAN.md §2), so it
is opened in WAL mode: the CLI must be usable while `icr serve` is running.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)


class DatabaseUnwritableError(Exception):
    """The database directory cannot be written to.

    Almost always a bind-mount ownership mismatch: the host directory belongs to
    one uid and the container runs as another. SQLite's own message for this is
    "unable to open database file", which says nothing useful, so the path and
    the running uid are surfaced here instead.
    """

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""


def utcnow() -> str:
    """Timestamps are stored as ISO-8601 UTC strings; SQLite has no date type."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _describe_identity() -> str:
    try:
        return f"uid {os.getuid()}, gid {os.getgid()}"
    except AttributeError:  # non-POSIX
        return "this process"


def in_container() -> bool:
    return Path("/.dockerenv").exists()


def _ownership_advice(parent: Path) -> str:
    """What to actually run to fix this.

    Inside a container the failing path is the *mount point*; running `chown` on
    it achieves nothing, because the ownership that matters belongs to the host
    directory mounted there. Naming the container path as the thing to chown --
    which an earlier version of this message did -- sends people in circles.
    """
    if not in_container():
        return (
            f"Fix ownership: sudo chown -R {os.getuid()}:{os.getgid()} {parent}"
        )
    return (
        f"{parent} is a mount point, so chowning it inside the container does nothing -- "
        f"fix the HOST directory mounted there instead. With the bundled compose file "
        f"that is ./data (or whatever ICR_DATA_DIR points at):\n"
        f"    sudo chown -R {os.getuid()}:{os.getgid()} ./data\n"
        f"Docker creates a missing bind-mount source directory owned by root, which is "
        f"the usual cause. Alternatively set ICR_UID/ICR_GID in your compose environment "
        f"to the uid that already owns the directory."
    )


def ensure_writable(db_path: Path) -> None:
    """Fail early and legibly if the database directory is not usable.

    WAL mode needs to create `-wal` and `-shm` siblings, so a writable
    *directory* is required, not just a writable file.
    """
    parent = db_path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DatabaseUnwritableError(
            f"Cannot create the database directory {parent} as {_describe_identity()}: "
            f"{exc}. {_ownership_advice(parent)}"
        ) from exc

    if not os.access(parent, os.W_OK | os.X_OK):
        raise DatabaseUnwritableError(
            f"The database directory {parent} is not writable by "
            f"{_describe_identity()}. {_ownership_advice(parent)}"
        )

    if db_path.exists() and not os.access(db_path, os.W_OK):
        raise DatabaseUnwritableError(
            f"The database file {db_path} exists but is not writable by "
            f"{_describe_identity()}. {_ownership_advice(parent)}"
        )


def connect(db_path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open the database, creating the parent directory if needed."""
    if not read_only:
        ensure_writable(db_path)

    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit transaction. `isolation_level=None` disables Python's implicit one."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _available_migrations() -> list[tuple[int, Path]]:
    migrations = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(path.name.split("_", 1)[0])
        migrations.append((version, path))
    return migrations


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    conn.executescript(_BOOTSTRAP)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row["version"] for row in rows}


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply every migration not yet recorded. Returns the versions applied.

    Each migration runs as one atomic script. `executescript` implicitly commits
    any open transaction before it starts, so the BEGIN/COMMIT has to live
    *inside* the script rather than around the call -- otherwise a failing
    migration would leave a half-applied schema behind.

    That alone is not enough: when a statement inside the script fails,
    `executescript` raises without unwinding, leaving the transaction open. The
    partial schema would then be visible to this connection, and the next
    `BEGIN IMMEDIATE` would fail with "cannot start a transaction within a
    transaction". Hence the explicit rollback.
    """
    done = applied_versions(conn)
    applied = []
    for version, path in _available_migrations():
        if version in done:
            continue
        log.info("Applying migration %s (%s)", version, path.name)
        body = path.read_text(encoding="utf-8")
        script = (
            "BEGIN;\n"
            f"{body}\n"
            "INSERT INTO schema_migrations (version, applied_at) "
            f"VALUES ({version:d}, '{utcnow()}');\n"
            "COMMIT;"
        )
        try:
            conn.executescript(script)
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            log.error("Migration %s failed and was rolled back.", version)
            raise
        applied.append(version)
    return applied


def backup(conn: sqlite3.Connection, destination: Path) -> Path:
    """Write a consistent snapshot of the database to `destination`.

    Uses SQLite's online backup API rather than a file copy. In WAL mode the
    committed data lives partly in the `-wal` sidecar, so copying `icr.sqlite3`
    alone while the service is running can silently produce a stale or torn
    snapshot. This is safe to run against a live database.
    """
    destination = destination.expanduser()
    ensure_writable(destination)

    target = sqlite3.connect(destination)
    try:
        with target:
            conn.backup(target)
    finally:
        target.close()

    log.info("Wrote backup to %s", destination)
    return destination


@contextmanager
def open_db(db_path: Path, *, migrate_on_open: bool = True) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        if migrate_on_open:
            migrate(conn)
        yield conn
    finally:
        conn.close()
