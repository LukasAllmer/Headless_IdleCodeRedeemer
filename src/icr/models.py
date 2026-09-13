"""Domain types shared across the storage, redeem and presentation layers."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum


class RedemptionStatus(StrEnum):
    """Outcome of one (account, code) redemption attempt.

    Terminal states are never retried; retryable ones are picked up again on a
    later tick until `max_redeem_attempts` is reached.
    """

    SUCCESS = "success"
    ALREADY_REDEEMED = "already_redeemed"
    EXPIRED = "expired"
    INVALID = "invalid"
    CANNOT_REDEEM = "cannot_redeem"

    PENDING = "pending"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL

    @property
    def is_retryable(self) -> bool:
        return not self.is_terminal


_TERMINAL = frozenset(
    {
        RedemptionStatus.SUCCESS,
        RedemptionStatus.ALREADY_REDEEMED,
        RedemptionStatus.EXPIRED,
        RedemptionStatus.INVALID,
        RedemptionStatus.CANNOT_REDEEM,
    }
)

TERMINAL_STATUSES = tuple(s.value for s in _TERMINAL)
RETRYABLE_STATUSES = (RedemptionStatus.PENDING.value, RedemptionStatus.FAILED.value)


@dataclass(slots=True)
class Account:
    id: int
    name: str
    user_id: str
    user_hash: str
    instance_id: str | None
    enabled: bool
    credentials_ok: bool
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Account:
        return cls(
            id=row["id"],
            name=row["name"],
            user_id=row["user_id"],
            user_hash=row["user_hash"],
            instance_id=row["instance_id"],
            enabled=bool(row["enabled"]),
            credentials_ok=bool(row["credentials_ok"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def __repr__(self) -> str:
        # Never let a hash reach a log or traceback via repr().
        return f"Account(id={self.id}, name={self.name!r})"


@dataclass(slots=True)
class Code:
    id: int
    code: str
    source: str
    source_ref: str | None
    first_seen_at: str
    note: str | None

    account_id: int | None = None
    """Which account may redeem this code, or None for all of them.

    Public codes -- Discord, manual entry -- are unscoped. A newsletter code
    mailed to one subscriber is single-use, so it is pinned to the account whose
    mailbox received it and never offered to the others.
    """

    @property
    def is_personal(self) -> bool:
        return self.account_id is not None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Code:
        return cls(
            id=row["id"],
            code=row["code"],
            source=row["source"],
            source_ref=row["source_ref"],
            first_seen_at=row["first_seen_at"],
            note=row["note"],
            account_id=row["account_id"],
        )


@dataclass(slots=True)
class WorkItem:
    """One outstanding (account, code) pair to attempt."""

    account_id: int
    account_name: str
    code_id: int
    code: str
    attempts: int


class MailboxAuth(StrEnum):
    """How to authenticate to a mailbox.

    Microsoft killed IMAP basic auth, so an Outlook/Office 365 mailbox needs
    XOAUTH2; `Mailbox.secret` then holds a refresh token rather than a password.
    """

    PASSWORD = "password"  # nosec B105 # names an auth method, not a credential
    MICROSOFT = "microsoft"


@dataclass(slots=True)
class Mailbox:
    id: int
    name: str
    account_id: int
    auth: MailboxAuth
    host: str
    port: int
    username: str
    secret: str | None
    oauth_client_id: str | None
    oauth_tenant: str | None
    enabled: bool
    last_polled_at: str | None
    last_error: str | None
    created_at: str
    updated_at: str

    #: Filled in when the row came from a query that joined `accounts`.
    account_name: str | None = None

    @property
    def needs_authorization(self) -> bool:
        return self.auth is MailboxAuth.MICROSOFT and not self.secret

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Mailbox:
        columns = row.keys()
        return cls(
            id=row["id"],
            name=row["name"],
            account_id=row["account_id"],
            auth=MailboxAuth(row["auth"]),
            host=row["host"],
            port=row["port"],
            username=row["username"],
            secret=row["secret"],
            oauth_client_id=row["oauth_client_id"],
            oauth_tenant=row["oauth_tenant"],
            enabled=bool(row["enabled"]),
            last_polled_at=row["last_polled_at"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            account_name=row["account_name"] if "account_name" in columns else None,
        )

    def __repr__(self) -> str:
        # Same reasoning as Account: never let the password or refresh token
        # reach a log or traceback via repr().
        return f"Mailbox(id={self.id}, name={self.name!r}, username={self.username!r})"


@dataclass(slots=True)
class Redemption:
    id: int
    account_id: int
    code_id: int
    status: RedemptionStatus
    attempts: int
    first_attempt_at: str
    last_attempt_at: str
    loot_json: str | None
    error: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Redemption:
        return cls(
            id=row["id"],
            account_id=row["account_id"],
            code_id=row["code_id"],
            status=RedemptionStatus(row["status"]),
            attempts=row["attempts"],
            first_attempt_at=row["first_attempt_at"],
            last_attempt_at=row["last_attempt_at"],
            loot_json=row["loot_json"],
            error=row["error"],
        )
