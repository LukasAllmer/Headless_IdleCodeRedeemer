from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

import icr.db as db_module
from icr.db import (
    DatabaseUnwritableError,
    backup,
    connect,
    ensure_writable,
    migrate,
    open_db,
    repo,
)

running_as_root = pytest.mark.skipif(
    hasattr(os, "getuid") and os.getuid() == 0,
    reason="root bypasses permission checks",
)


# --------------------------------------------------------------------------
# writability preflight
# --------------------------------------------------------------------------


def test_creates_missing_parent_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deeper" / "icr.sqlite3"
    ensure_writable(target)
    assert target.parent.is_dir()


@running_as_root
def test_unwritable_directory_names_the_fix(tmp_path: Path, monkeypatch) -> None:
    """The bind-mount ownership mismatch. SQLite's own error for this is
    'unable to open database file', which tells you nothing."""
    monkeypatch.setattr(db_module, "in_container", lambda: False)
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        with pytest.raises(DatabaseUnwritableError) as excinfo:
            ensure_writable(locked / "icr.sqlite3")
        message = str(excinfo.value)
        assert "not writable" in message
        assert f"chown -R {os.getuid()}:{os.getgid()} {locked}" in message
    finally:
        locked.chmod(0o700)


@running_as_root
def test_in_container_points_at_the_host_directory(tmp_path: Path, monkeypatch) -> None:
    """Inside a container the failing path is a mount point. Telling the user to
    chown it achieves nothing -- the ownership that matters is the host
    directory mounted there."""
    monkeypatch.setattr(db_module, "in_container", lambda: True)
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        with pytest.raises(DatabaseUnwritableError) as excinfo:
            ensure_writable(locked / "icr.sqlite3")
        message = str(excinfo.value)
        assert "./data" in message
        assert "mount point" in message
        assert "ICR_UID" in message
        # Must not tell them to chown the container-side path.
        assert f"chown -R {os.getuid()}:{os.getgid()} {locked}" not in message
    finally:
        locked.chmod(0o700)


@running_as_root
def test_unwritable_existing_file_is_reported(tmp_path: Path) -> None:
    db = tmp_path / "icr.sqlite3"
    db.touch(mode=0o400)
    with pytest.raises(DatabaseUnwritableError, match="not writable"):
        ensure_writable(db)


def test_connect_surfaces_the_friendly_error(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        if os.access(locked, os.W_OK):
            pytest.skip("root bypasses permission checks")
        with pytest.raises(DatabaseUnwritableError):
            connect(locked / "icr.sqlite3")
    finally:
        locked.chmod(0o700)


# --------------------------------------------------------------------------
# backup
# --------------------------------------------------------------------------


def test_backup_snapshot_contains_the_data(tmp_path: Path) -> None:
    source = tmp_path / "icr.sqlite3"
    with open_db(source) as conn:
        repo.add_account(conn, name="main", user_id="u1", user_hash="h1")
        repo.add_code(conn, "ABCDEFGHIJKL", source="manual")

        destination = backup(conn, tmp_path / "snapshot.sqlite3")

    assert destination.exists()
    copy = sqlite3.connect(destination)
    try:
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert copy.execute("SELECT name FROM accounts").fetchone()[0] == "main"
        assert copy.execute("SELECT code FROM codes").fetchone()[0] == "ABCDEFGHIJKL"
    finally:
        copy.close()


def test_backup_captures_writes_still_in_the_wal(tmp_path: Path) -> None:
    """The reason this uses the online backup API rather than copying the file:
    in WAL mode, committed rows may live only in the -wal sidecar."""
    source = tmp_path / "icr.sqlite3"
    with open_db(source) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        repo.add_code(conn, "INWALFILE123", source="manual")
        assert source.with_name(source.name + "-wal").exists()

        destination = backup(conn, tmp_path / "snapshot.sqlite3")

        copy = sqlite3.connect(destination)
        try:
            assert copy.execute("SELECT COUNT(*) FROM codes").fetchone()[0] == 1
        finally:
            copy.close()


def test_backup_creates_missing_destination_directory(tmp_path: Path) -> None:
    with open_db(tmp_path / "icr.sqlite3") as conn:
        destination = backup(conn, tmp_path / "backups" / "snapshot.sqlite3")
    assert destination.exists()


# --------------------------------------------------------------------------
# migrations
# --------------------------------------------------------------------------


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    conn = connect(tmp_path / "icr.sqlite3")
    try:
        first = migrate(conn)
        assert first == sorted(first) and first[0] == 1
        assert migrate(conn) == []
    finally:
        conn.close()


def test_failed_migration_leaves_no_partial_schema(tmp_path: Path, monkeypatch) -> None:
    """Each migration runs as one atomic script -- `executescript` implicitly
    commits, so the BEGIN/COMMIT has to live inside it."""
    bad = tmp_path / "999_bad.sql"
    bad.write_text("CREATE TABLE ok_table (id INTEGER);\nCREATE TABLE ok_table (id INTEGER);\n")

    conn = connect(tmp_path / "icr.sqlite3")
    try:
        real = migrate(conn)
        monkeypatch.setattr(db_module, "_available_migrations", lambda: [(999, bad)])
        with pytest.raises(sqlite3.OperationalError):
            migrate(conn)

        tables = {
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "ok_table" not in tables
        applied = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
        assert applied == set(real)
    finally:
        conn.close()
