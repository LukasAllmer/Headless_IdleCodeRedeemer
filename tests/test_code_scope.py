"""Per-account codes.

A newsletter code is single-use: redeeming it on the wrong account burns it and
leaves the subscriber with nothing. `codes.account_id` is what stops that, so
these tests are about who a code is offered to, not about redeeming it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import icr.db as db_module
from icr.db import connect, migrate, repo


def accounts(conn: sqlite3.Connection) -> tuple[int, int]:
    a = repo.add_account(conn, name="main", user_id="u1", user_hash="h1")
    b = repo.add_account(conn, name="alt", user_id="u2", user_hash="h2")
    return a.id, b.id


def pairs(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    return {
        (item.account_name, item.code)
        for item in repo.outstanding_work(conn, max_attempts=3)
    }


def test_public_code_is_offered_to_every_account(conn: sqlite3.Connection) -> None:
    accounts(conn)
    repo.add_code(conn, "PUBLICCODE12", source="discord")
    assert pairs(conn) == {("main", "PUBLICCODE12"), ("alt", "PUBLICCODE12")}


def test_personal_code_is_offered_only_to_its_account(conn: sqlite3.Connection) -> None:
    main_id, _ = accounts(conn)
    repo.add_code(conn, "PERSONALCODE", source="email", account_id=main_id)
    assert pairs(conn) == {("main", "PERSONALCODE")}


def test_personal_and_public_codes_coexist(conn: sqlite3.Connection) -> None:
    main_id, _ = accounts(conn)
    repo.add_code(conn, "PUBLICCODE12", source="discord")
    repo.add_code(conn, "PERSONALCODE", source="email", account_id=main_id)
    assert pairs(conn) == {
        ("main", "PUBLICCODE12"),
        ("alt", "PUBLICCODE12"),
        ("main", "PERSONALCODE"),
    }


def test_a_public_sighting_widens_a_personal_code(conn: sqlite3.Connection) -> None:
    """If a code arrives by mail and then turns up on Discord it was public all
    along. Widening only ever adds work; it cannot undo a redemption."""
    main_id, _ = accounts(conn)
    repo.add_code(conn, "WASPERSONAL1", source="email", account_id=main_id)
    assert pairs(conn) == {("main", "WASPERSONAL1")}

    stored, was_new = repo.add_code(conn, "WASPERSONAL1", source="discord")

    assert was_new is False
    assert stored.account_id is None
    assert pairs(conn) == {("main", "WASPERSONAL1"), ("alt", "WASPERSONAL1")}


def test_a_public_code_is_never_narrowed(conn: sqlite3.Connection) -> None:
    """The other direction would take a code away from accounts that can use it."""
    main_id, _ = accounts(conn)
    repo.add_code(conn, "PUBLICCODE12", source="discord")

    stored, was_new = repo.add_code(
        conn, "PUBLICCODE12", source="email", account_id=main_id
    )

    assert was_new is False
    assert stored.account_id is None
    assert pairs(conn) == {("main", "PUBLICCODE12"), ("alt", "PUBLICCODE12")}


def test_disabled_account_gets_nothing_even_for_its_own_code(
    conn: sqlite3.Connection,
) -> None:
    main_id, _ = accounts(conn)
    repo.add_code(conn, "PERSONALCODE", source="email", account_id=main_id)
    repo.set_account_enabled(conn, "main", False)
    assert pairs(conn) == set()


def test_removing_an_account_takes_its_personal_codes_with_it(
    conn: sqlite3.Connection,
) -> None:
    main_id, _ = accounts(conn)
    repo.add_code(conn, "PUBLICCODE12", source="discord")
    repo.add_code(conn, "PERSONALCODE", source="email", account_id=main_id)

    repo.remove_account(conn, "main")

    remaining = {c.code for c in repo.list_codes(conn)}
    assert remaining == {"PUBLICCODE12"}, "the public code must survive"


# --------------------------------------------------------------------------
# upgrading a database that predates this feature
# --------------------------------------------------------------------------


def test_migration_002_preserves_an_existing_database(tmp_path: Path, monkeypatch) -> None:
    """Migration 002 is ALTER TABLE ADD COLUMN plus a new table -- nothing is
    rebuilt, so every row an existing install has survives untouched, and its
    codes stay public because NULL is what they always meant."""
    path = tmp_path / "icr.sqlite3"

    only_v1 = [m for m in db_module._available_migrations() if m[0] == 1]
    monkeypatch.setattr(db_module, "_available_migrations", lambda: only_v1)

    old = connect(path)
    try:
        assert migrate(old) == [1]
        # Written the way the old code wrote it -- `repo` now knows about a
        # column this schema does not have yet.
        old.execute(
            "INSERT INTO accounts (name, user_id, user_hash, enabled, credentials_ok,"
            " created_at, updated_at) VALUES ('main', 'u1', 'h1', 1, 1, 'then', 'then')"
        )
        old.execute(
            "INSERT INTO codes (code, source, source_ref, first_seen_at, note)"
            " VALUES ('LEGACYCODE12', 'discord', '42', 'then', 'old')"
        )
        old.execute(
            "INSERT INTO redemptions (account_id, code_id, status, attempts,"
            " first_attempt_at, last_attempt_at) VALUES (1, 1, 'success', 1, 'then', 'then')"
        )
    finally:
        old.close()

    monkeypatch.undo()

    upgraded = connect(path)
    try:
        assert migrate(upgraded) == [2]

        assert [a.name for a in repo.list_accounts(upgraded)] == ["main"]
        migrated_code = repo.get_code(upgraded, "LEGACYCODE12")
        assert migrated_code is not None
        assert migrated_code.source_ref == "42"
        assert migrated_code.note == "old"
        assert migrated_code.account_id is None, "existing codes stay public"

        entries = repo.history(upgraded)
        assert len(entries) == 1
        assert entries[0].status is repo.RedemptionStatus.SUCCESS

        assert repo.list_mailboxes(upgraded) == []
        assert upgraded.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        upgraded.close()
