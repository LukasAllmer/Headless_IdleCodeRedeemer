"""Email source: newsletter codes, one mailbox per game account.

Codename Entertainment mails a code to each newsletter subscriber. Unlike a
Discord announcement those are single-use, so a code found here is pinned to the
account whose mailbox received it -- redeeming it on a different account would
consume it on the wrong one and leave the subscriber with nothing.

That binding is the whole reason `mailboxes.account_id` is NOT NULL and
`DiscoveredCode.account_id` exists.

One source object covers every configured mailbox. A mailbox that fails does not
stop the others; its error is written to its row so `icr mailbox list` can show
it. Only when every mailbox fails does the source report failure as a whole.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import UTC, date, datetime, timedelta

from icr.config import Settings
from icr.db import repo
from icr.mail import (
    FolderCursor,
    MailError,
    OAuthError,
    ScanResult,
    exchange_refresh_token,
    scan_mailbox,
)
from icr.models import Mailbox, MailboxAuth
from icr.sources.base import DiscoveredCode, SourceError, extract_codes, register

log = logging.getLogger(__name__)

#: One key per mailbox, holding a JSON object of per-folder UID positions.
#:
#: Older installations wrote a bare ISO date here instead -- how far the whole
#: mailbox had been read. That value is still understood on the way in and is
#: used as the starting window, after which it is replaced by UID positions.
KV_CURSOR_PREFIX = "email:mailbox:"


def cursor_key(mailbox_id: int) -> str:
    return f"{KV_CURSOR_PREFIX}{mailbox_id}"


def _load_cursors(raw: str | None) -> tuple[dict[str, FolderCursor], date | None]:
    """Decode a stored cursor into (per-folder UID positions, legacy date)."""
    if not raw:
        return {}, None
    try:
        payload = json.loads(raw)
    except ValueError:
        # Pre-UID format: a bare ISO date covering the whole mailbox.
        try:
            return {}, date.fromisoformat(raw)
        except ValueError:
            return {}, None
    if not isinstance(payload, dict):
        return {}, None

    cursors: dict[str, FolderCursor] = {}
    for folder, entry in (payload.get("folders") or {}).items():
        try:
            cursors[folder] = FolderCursor(
                uidvalidity=str(entry["uidvalidity"]), uid=int(entry["uid"])
            )
        except (TypeError, KeyError, ValueError):
            log.warning("Ignoring unreadable cursor for folder %r.", folder)
    return cursors, None


def _dump_cursors(cursors: dict[str, FolderCursor]) -> str:
    return json.dumps(
        {
            "folders": {
                folder: {"uidvalidity": c.uidvalidity, "uid": c.uid}
                for folder, c in sorted(cursors.items())
            }
        }
    )


class EmailSource:
    name = "email"

    def enabled(self, settings: Settings) -> bool:
        return settings.email_enabled

    def position(self, conn: sqlite3.Connection) -> str | None:
        stored = repo.kv_list_prefix(conn, KV_CURSOR_PREFIX)
        if not stored:
            return None
        mailboxes = {m.id: m.name for m in repo.list_mailboxes(conn)}
        parts = []
        for key, value in sorted(stored.items()):
            raw_id = key[len(KV_CURSOR_PREFIX) :]
            label = mailboxes.get(int(raw_id), raw_id) if raw_id.isdigit() else raw_id
            cursors, legacy = _load_cursors(value)
            if cursors:
                highest = max(c.uid for c in cursors.values())
                parts.append(f"{label}={len(cursors)} folder(s), highest uid {highest}")
            elif legacy:
                parts.append(f"{label}=read to {legacy.isoformat()} (no uid position yet)")
        return ", ".join(parts) if parts else None

    def reset(self, conn: sqlite3.Connection) -> None:
        repo.kv_delete_prefix(conn, KV_CURSOR_PREFIX)

    async def poll(self, conn: sqlite3.Connection, settings: Settings) -> list[DiscoveredCode]:
        mailboxes = repo.list_mailboxes(conn, enabled_only=True)
        if not mailboxes:
            raise SourceError(
                "ICR_EMAIL_ENABLED is true but no mailboxes are configured. "
                "Add one with `icr mailbox add`."
            )

        discovered: list[DiscoveredCode] = []
        failures: list[str] = []

        for mailbox in mailboxes:
            try:
                discovered.extend(await self._poll_one(conn, settings, mailbox))
            except (MailError, OAuthError, SourceError) as exc:
                # Understood and actionable: a rejected password, an expired
                # token, an unreachable host.
                log.error("Mailbox %s: %s", mailbox.name, exc)
                repo.record_mailbox_poll(conn, mailbox.id, error=str(exc))
                failures.append(f"{mailbox.name}: {exc}")
            except Exception as exc:
                log.exception("Mailbox %s failed unexpectedly", mailbox.name)
                repo.record_mailbox_poll(conn, mailbox.id, error=str(exc))
                failures.append(f"{mailbox.name}: {exc}")
            else:
                repo.record_mailbox_poll(conn, mailbox.id, error=None)

        if failures and len(failures) == len(mailboxes):
            raise SourceError("; ".join(failures))
        return discovered

    async def _poll_one(
        self, conn: sqlite3.Connection, settings: Settings, mailbox: Mailbox
    ) -> list[DiscoveredCode]:
        cursors, legacy = _load_cursors(repo.kv_get(conn, cursor_key(mailbox.id)))
        since = self._since(settings, legacy)
        result = await read_mailbox(
            conn,
            settings,
            mailbox,
            cursors=cursors,
            since=since,
            rescan_since=_today() - timedelta(days=settings.email_rescan_days),
        )

        discovered = [
            DiscoveredCode(
                code=code,
                source_ref=f"{mailbox.name}:{message.folder}:{message.uid}",
                note=f"email to {mailbox.username} -- {message.subject or '(no subject)'}",
                account_id=mailbox.account_id,
            )
            for message in result.messages
            for code in extract_codes(message.text)
        ]

        repo.kv_set(conn, cursor_key(mailbox.id), _dump_cursors(result.cursors))
        log.info(
            "Mailbox %s (account %s): %d new message(s), %d code(s) found, "
            "%d folder(s) tracked",
            mailbox.name,
            mailbox.account_name or mailbox.account_id,
            len(result.messages),
            len(discovered),
            len(result.cursors),
        )
        return discovered

    def _since(self, settings: Settings, legacy: date | None) -> date | None:
        """The date bound for folders with no UID position yet.

        Only ever applies to a folder's first read. A mailbox carried over from
        the date-cursor format starts from wherever that cursor had reached,
        rather than re-reading its whole history once on upgrade.
        """
        if legacy:
            return legacy - timedelta(days=settings.email_rescan_days)
        if settings.email_initial_scan_days:
            return _today() - timedelta(days=settings.email_initial_scan_days)
        return None  # first look at this mailbox: read all of it


async def resolve_credentials(
    conn: sqlite3.Connection, settings: Settings, mailbox: Mailbox
) -> tuple[str | None, str | None]:
    """Return (password, access_token); exactly one is set.

    Shared with `icr mailbox test`, so that the command exercises the same token
    refresh the scheduled poll does instead of a lookalike.
    """
    if mailbox.auth is not MailboxAuth.MICROSOFT:
        if not mailbox.secret:
            raise SourceError(
                f"Mailbox {mailbox.name} has no password stored. "
                f"Re-add it with `icr mailbox add --name {mailbox.name} ...`."
            )
        return mailbox.secret, None

    if not mailbox.secret:
        raise SourceError(
            f"Mailbox {mailbox.name} has not been authorized yet. "
            f"Run `icr mailbox authorize {mailbox.name}`."
        )
    client_id = mailbox.oauth_client_id or settings.email_oauth_client_id
    if not client_id:
        raise SourceError(
            f"Mailbox {mailbox.name} is a Microsoft account but no client id is "
            "configured. Set ICR_EMAIL_OAUTH_CLIENT_ID or pass --client-id."
        )

    tokens = await exchange_refresh_token(
        mailbox.secret,
        client_id=client_id,
        tenant=mailbox.oauth_tenant or settings.email_oauth_tenant,
        timeout=settings.http_timeout_seconds,
    )
    if tokens.refresh_token and tokens.refresh_token != mailbox.secret:
        # Microsoft rotates these. Dropping the new one works until the old one
        # is invalidated, and then the mailbox dies for no visible reason.
        repo.update_mailbox_secret(conn, mailbox.id, tokens.refresh_token)
    return None, tokens.access_token


async def read_mailbox(
    conn: sqlite3.Connection,
    settings: Settings,
    mailbox: Mailbox,
    *,
    cursors: dict[str, FolderCursor] | None = None,
    since: date | None,
    rescan_since: date | None = None,
) -> ScanResult:
    """Authenticate and scan, off the event loop."""
    password, access_token = await resolve_credentials(conn, settings, mailbox)
    return await asyncio.to_thread(
        scan_mailbox,
        host=mailbox.host,
        port=mailbox.port,
        username=mailbox.username,
        password=password,
        access_token=access_token,
        senders=settings.sender_list,
        cursors=cursors,
        since=since,
        rescan_since=rescan_since,
        limit_per_folder=settings.email_max_messages_per_folder,
        timeout=settings.email_timeout_seconds,
    )


def _today() -> date:
    return datetime.now(UTC).date()


register(EmailSource())
