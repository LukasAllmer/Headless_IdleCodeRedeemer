"""All SQL for the application. Nothing outside this module writes queries."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from icr.db import transaction, utcnow
from icr.logging_setup import register_secret
from icr.models import (
    RETRYABLE_STATUSES,
    Account,
    Code,
    Mailbox,
    MailboxAuth,
    Redemption,
    RedemptionStatus,
    WorkItem,
)


class DuplicateAccountError(Exception):
    pass


class AccountNotFoundError(Exception):
    pass


class DuplicateMailboxError(Exception):
    pass


class MailboxNotFoundError(Exception):
    pass


# --------------------------------------------------------------------------
# accounts
# --------------------------------------------------------------------------


def add_account(
    conn: sqlite3.Connection,
    *,
    name: str,
    user_id: str,
    user_hash: str,
    instance_id: str | None = None,
) -> Account:
    now = utcnow()
    try:
        with transaction(conn):
            cur = conn.execute(
                """
                INSERT INTO accounts
                    (name, user_id, user_hash, instance_id, enabled, credentials_ok,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, 1, ?, ?)
                """,
                (name, user_id, user_hash, instance_id, now, now),
            )
    except sqlite3.IntegrityError as exc:
        raise DuplicateAccountError(
            f"An account named {name!r} or with these credentials already exists."
        ) from exc
    account = get_account(conn, cur.lastrowid)  # type: ignore[arg-type]
    assert account is not None
    return account


def get_account(conn: sqlite3.Connection, account_id: int) -> Account | None:
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return Account.from_row(row) if row else None


def get_account_by_name(conn: sqlite3.Connection, name: str) -> Account | None:
    row = conn.execute("SELECT * FROM accounts WHERE name = ?", (name,)).fetchone()
    return Account.from_row(row) if row else None


def require_account(conn: sqlite3.Connection, name: str) -> Account:
    account = get_account_by_name(conn, name)
    if account is None:
        raise AccountNotFoundError(f"No account named {name!r}.")
    return account


def list_accounts(conn: sqlite3.Connection, *, enabled_only: bool = False) -> list[Account]:
    sql = "SELECT * FROM accounts"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY name"
    return [Account.from_row(r) for r in conn.execute(sql).fetchall()]


def load_secrets(conn: sqlite3.Connection) -> None:
    """Register every stored credential with the log redaction filter.

    Called once at startup so that a hash, mailbox password or refresh token
    appearing in a traceback or a debug request dump is scrubbed before it
    reaches disk.
    """
    for row in conn.execute("SELECT user_hash FROM accounts").fetchall():
        register_secret(row["user_hash"])
    for row in conn.execute(
        "SELECT secret FROM mailboxes WHERE secret IS NOT NULL"
    ).fetchall():
        register_secret(row["secret"])


def set_account_enabled(conn: sqlite3.Connection, name: str, enabled: bool) -> Account:
    account = require_account(conn, name)
    with transaction(conn):
        conn.execute(
            "UPDATE accounts SET enabled = ?, updated_at = ? WHERE id = ?",
            (int(enabled), utcnow(), account.id),
        )
    return require_account(conn, name)


def update_instance_id(conn: sqlite3.Connection, account_id: int, instance_id: str) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE accounts SET instance_id = ?, updated_at = ? WHERE id = ?",
            (instance_id, utcnow(), account_id),
        )


def set_credentials_ok(conn: sqlite3.Connection, account_id: int, ok: bool) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE accounts SET credentials_ok = ?, updated_at = ? WHERE id = ?",
            (int(ok), utcnow(), account_id),
        )


def update_credentials(
    conn: sqlite3.Connection, account_id: int, *, user_id: str, user_hash: str
) -> None:
    with transaction(conn):
        conn.execute(
            """
            UPDATE accounts
            SET user_id = ?, user_hash = ?, credentials_ok = 1, instance_id = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (user_id, user_hash, utcnow(), account_id),
        )
    register_secret(user_hash)


def remove_account(conn: sqlite3.Connection, name: str) -> None:
    account = require_account(conn, name)
    with transaction(conn):
        conn.execute("DELETE FROM accounts WHERE id = ?", (account.id,))


# --------------------------------------------------------------------------
# codes
# --------------------------------------------------------------------------


def add_code(
    conn: sqlite3.Connection,
    code: str,
    *,
    source: str,
    source_ref: str | None = None,
    note: str | None = None,
    account_id: int | None = None,
) -> tuple[Code, bool]:
    """Insert a code, ignoring duplicates. Returns (code, was_new).

    `account_id` pins the code to one account; None offers it to all of them.
    A code already on file is never re-scoped *narrower* -- but a personal code
    that later turns up from a public source was evidently public all along, so
    that direction widens it. Widening can only ever add work, never discard a
    redemption that already happened.
    """
    with transaction(conn):
        cur = conn.execute(
            """
            INSERT INTO codes (code, source, source_ref, first_seen_at, note, account_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (code) DO NOTHING
            """,
            (code, source, source_ref, utcnow(), note, account_id),
        )
        was_new = cur.rowcount > 0
        if not was_new and account_id is None:
            conn.execute(
                "UPDATE codes SET account_id = NULL WHERE code = ? AND account_id IS NOT NULL",
                (code,),
            )
    row = conn.execute("SELECT * FROM codes WHERE code = ?", (code,)).fetchone()
    return Code.from_row(row), was_new


def add_codes(
    conn: sqlite3.Connection,
    codes: list[str],
    *,
    source: str,
    source_ref: str | None = None,
    note: str | None = None,
    account_id: int | None = None,
) -> list[Code]:
    """Bulk insert. Returns only the codes that were newly added."""
    added = []
    for code in codes:
        stored, was_new = add_code(
            conn,
            code,
            source=source,
            source_ref=source_ref,
            note=note,
            account_id=account_id,
        )
        if was_new:
            added.append(stored)
    return added


def list_codes(conn: sqlite3.Connection, *, limit: int | None = None) -> list[Code]:
    sql = "SELECT * FROM codes ORDER BY first_seen_at DESC, id DESC"
    params: tuple[int, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    return [Code.from_row(r) for r in conn.execute(sql, params).fetchall()]


def get_code(conn: sqlite3.Connection, code: str) -> Code | None:
    row = conn.execute("SELECT * FROM codes WHERE code = ?", (code,)).fetchone()
    return Code.from_row(row) if row else None


# --------------------------------------------------------------------------
# redemptions -- the work queue
# --------------------------------------------------------------------------


def outstanding_work(
    conn: sqlite3.Connection,
    *,
    max_attempts: int,
    account_name: str | None = None,
    code: str | None = None,
) -> list[WorkItem]:
    """Every (account, code) pair still worth attempting.

    This single query replaces the extension's `pendingCodes`/`redeemedCodes`
    array juggling. A terminal redemption row means "never again"; a retryable
    one comes back until `attempts` hits the ceiling.

    `c.account_id IS NULL` is the public case and pairs with every account; a
    personal code from a newsletter pairs only with its own.
    """
    retryable = ",".join("?" * len(RETRYABLE_STATUSES))
    sql = f"""
        SELECT a.id   AS account_id,
               a.name AS account_name,
               c.id   AS code_id,
               c.code AS code,
               COALESCE(r.attempts, 0) AS attempts
        FROM accounts a
        CROSS JOIN codes c
        LEFT JOIN redemptions r ON r.account_id = a.id AND r.code_id = c.id
        WHERE a.enabled = 1
          AND a.credentials_ok = 1
          AND (c.account_id IS NULL OR c.account_id = a.id)
          AND (
                r.id IS NULL
                OR (r.status IN ({retryable}) AND r.attempts < ?)
              )
    """
    params: list[object] = [*RETRYABLE_STATUSES, max_attempts]

    if account_name is not None:
        sql += " AND a.name = ?"
        params.append(account_name)
    if code is not None:
        sql += " AND c.code = ?"
        params.append(code)
    sql += " ORDER BY a.name, c.first_seen_at, c.id"

    rows = conn.execute(sql, params).fetchall()
    return [
        WorkItem(
            account_id=r["account_id"],
            account_name=r["account_name"],
            code_id=r["code_id"],
            code=r["code"],
            attempts=r["attempts"],
        )
        for r in rows
    ]


def record_redemption(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    code_id: int,
    status: RedemptionStatus,
    loot_json: str | None = None,
    error: str | None = None,
) -> None:
    """Upsert the ledger row, incrementing `attempts` on repeat attempts."""
    now = utcnow()
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO redemptions
                (account_id, code_id, status, attempts, first_attempt_at,
                 last_attempt_at, loot_json, error)
            VALUES (?, ?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT (account_id, code_id) DO UPDATE SET
                status          = excluded.status,
                attempts        = redemptions.attempts + 1,
                last_attempt_at = excluded.last_attempt_at,
                loot_json       = excluded.loot_json,
                error           = excluded.error
            """,
            (account_id, code_id, status.value, now, now, loot_json, error),
        )


@dataclass(slots=True)
class HistoryEntry:
    account_name: str
    code: str
    status: RedemptionStatus
    attempts: int
    last_attempt_at: str
    error: str | None
    loot_json: str | None


def history(
    conn: sqlite3.Connection,
    *,
    account_name: str | None = None,
    limit: int = 50,
) -> list[HistoryEntry]:
    sql = """
        SELECT a.name AS account_name, c.code AS code, r.status, r.attempts,
               r.last_attempt_at, r.error, r.loot_json
        FROM redemptions r
        JOIN accounts a ON a.id = r.account_id
        JOIN codes c    ON c.id = r.code_id
    """
    params: list[object] = []
    if account_name is not None:
        sql += " WHERE a.name = ?"
        params.append(account_name)
    sql += " ORDER BY r.last_attempt_at DESC, r.id DESC LIMIT ?"
    params.append(limit)

    return [
        HistoryEntry(
            account_name=r["account_name"],
            code=r["code"],
            status=RedemptionStatus(r["status"]),
            attempts=r["attempts"],
            last_attempt_at=r["last_attempt_at"],
            error=r["error"],
            loot_json=r["loot_json"],
        )
        for r in conn.execute(sql, params).fetchall()
    ]


def get_redemption(
    conn: sqlite3.Connection, *, account_id: int, code_id: int
) -> Redemption | None:
    row = conn.execute(
        "SELECT * FROM redemptions WHERE account_id = ? AND code_id = ?",
        (account_id, code_id),
    ).fetchone()
    return Redemption.from_row(row) if row else None


def status_counts(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """Per-account tally of redemption statuses, for `icr status` and the dashboard."""
    rows = conn.execute(
        """
        SELECT a.name AS account_name, r.status, COUNT(*) AS n
        FROM redemptions r
        JOIN accounts a ON a.id = r.account_id
        GROUP BY a.name, r.status
        """
    ).fetchall()
    out: dict[str, dict[str, int]] = {}
    for row in rows:
        out.setdefault(row["account_name"], {})[row["status"]] = row["n"]
    return out


# --------------------------------------------------------------------------
# mailboxes -- the email source's accounts
# --------------------------------------------------------------------------

_MAILBOX_SELECT = """
    SELECT m.*, a.name AS account_name
    FROM mailboxes m
    JOIN accounts a ON a.id = m.account_id
"""


def add_mailbox(
    conn: sqlite3.Connection,
    *,
    name: str,
    account_id: int,
    auth: MailboxAuth,
    host: str,
    port: int,
    username: str,
    secret: str | None = None,
    oauth_client_id: str | None = None,
    oauth_tenant: str | None = None,
) -> Mailbox:
    now = utcnow()
    try:
        with transaction(conn):
            conn.execute(
                """
                INSERT INTO mailboxes
                    (name, account_id, auth, host, port, username, secret,
                     oauth_client_id, oauth_tenant, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    name,
                    account_id,
                    auth.value,
                    host,
                    port,
                    username,
                    secret,
                    oauth_client_id,
                    oauth_tenant,
                    now,
                    now,
                ),
            )
    except sqlite3.IntegrityError as exc:
        raise DuplicateMailboxError(f"A mailbox named {name!r} already exists.") from exc
    if secret:
        register_secret(secret)
    return require_mailbox(conn, name)


def get_mailbox_by_name(conn: sqlite3.Connection, name: str) -> Mailbox | None:
    row = conn.execute(f"{_MAILBOX_SELECT} WHERE m.name = ?", (name,)).fetchone()
    return Mailbox.from_row(row) if row else None


def require_mailbox(conn: sqlite3.Connection, name: str) -> Mailbox:
    mailbox = get_mailbox_by_name(conn, name)
    if mailbox is None:
        raise MailboxNotFoundError(f"No mailbox named {name!r}.")
    return mailbox


def list_mailboxes(conn: sqlite3.Connection, *, enabled_only: bool = False) -> list[Mailbox]:
    sql = _MAILBOX_SELECT
    if enabled_only:
        sql += " WHERE m.enabled = 1"
    sql += " ORDER BY m.name"
    return [Mailbox.from_row(r) for r in conn.execute(sql).fetchall()]


def update_mailbox_secret(conn: sqlite3.Connection, mailbox_id: int, secret: str) -> None:
    """Store a password, or a rotated OAuth refresh token."""
    with transaction(conn):
        conn.execute(
            "UPDATE mailboxes SET secret = ?, updated_at = ? WHERE id = ?",
            (secret, utcnow(), mailbox_id),
        )
    register_secret(secret)


def set_mailbox_enabled(conn: sqlite3.Connection, name: str, enabled: bool) -> Mailbox:
    mailbox = require_mailbox(conn, name)
    with transaction(conn):
        conn.execute(
            "UPDATE mailboxes SET enabled = ?, updated_at = ? WHERE id = ?",
            (int(enabled), utcnow(), mailbox.id),
        )
    return require_mailbox(conn, name)


def record_mailbox_poll(
    conn: sqlite3.Connection, mailbox_id: int, *, error: str | None = None
) -> None:
    """Remember when a mailbox was last read, and why it failed if it did.

    Kept on the row rather than only in the log so `icr mailbox list` can show a
    stale token or a rejected password without anyone reading journald.
    """
    with transaction(conn):
        conn.execute(
            "UPDATE mailboxes SET last_polled_at = ?, last_error = ?, updated_at = ? WHERE id = ?",
            (utcnow(), error, utcnow(), mailbox_id),
        )


def remove_mailbox(conn: sqlite3.Connection, name: str) -> None:
    mailbox = require_mailbox(conn, name)
    with transaction(conn):
        conn.execute("DELETE FROM mailboxes WHERE id = ?", (mailbox.id,))


# --------------------------------------------------------------------------
# kv -- source cursors, cached play server
# --------------------------------------------------------------------------


def kv_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value,
                                            updated_at = excluded.updated_at
            """,
            (key, value, utcnow()),
        )


def kv_delete(conn: sqlite3.Connection, key: str) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM kv WHERE key = ?", (key,))


def kv_delete_prefix(conn: sqlite3.Connection, prefix: str) -> int:
    """Drop every key under a prefix. The email source keeps one cursor per
    mailbox, so resetting it is a family of keys rather than a single one."""
    with transaction(conn):
        cur = conn.execute(
            "DELETE FROM kv WHERE key LIKE ? ESCAPE '\\'",
            (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",),
        )
    return cur.rowcount


def kv_list_prefix(conn: sqlite3.Connection, prefix: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT key, value FROM kv WHERE key LIKE ? ESCAPE '\\'",
        (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",),
    ).fetchall()
    return {r["key"]: r["value"] for r in rows}
