"""IMAP scanning.

Blocking on purpose: `imaplib` underneath `imap-tools` is synchronous, and the
caller runs this on a worker thread. Wrapping it in an async IMAP client would
buy nothing -- a poll happens every few minutes and talks to one server at a
time.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from datetime import date

from imap_tools import AND, OR, MailBox, UidRange
from imap_tools.errors import ImapToolsError, MailboxLoginError
from imap_tools.query import LogicOperator

log = logging.getLogger(__name__)

#: Folders the server lists but will not let anyone select -- container nodes in
#: a hierarchy, mostly. Selecting one is an error, not a surprise.
_UNSELECTABLE = {"\\noselect", "\\nonexistent"}

_SCRIPT_OR_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")


class MailError(Exception):
    """An IMAP failure worth reporting to the user verbatim."""


@dataclass(slots=True)
class FoundMessage:
    folder: str
    uid: str
    subject: str
    sender: str
    date: str
    body: str

    @property
    def text(self) -> str:
        return f"{self.subject}\n{self.body}"


@dataclass(slots=True)
class FolderCursor:
    """How far one folder has been read.

    `uid` is the highest UID scanned. UIDs ascend within a folder and are never
    reused, which is what makes "only what arrived since" expressible to the
    server -- unlike a date, which IMAP only understands to a day's resolution.

    `uidvalidity` is the qualifier that makes the number mean anything. A server
    that recreates or migrates a folder bumps it, and every UID underneath it
    becomes meaningless; storing the two together is the only way to notice.
    """

    uidvalidity: str
    uid: int


@dataclass(slots=True)
class ScanResult:
    messages: list[FoundMessage]
    cursors: dict[str, FolderCursor]
    """The complete new cursor map, ready to persist as-is.

    Folders that could not be read keep whatever they had: a folder that is
    briefly unselectable should resume, not rescan.
    """


def strip_html(markup: str) -> str:
    """Reduce an HTML body to visible text.

    Tags become spaces rather than nothing: `<td>ABCD</td><td>EFGH</td>` must not
    collapse into one twelve-character run that looks like a code. Attributes go
    with the tag, which conveniently takes tracking URLs out of the picture --
    `extract_codes` upper-cases before matching, so a lowercase hex tracking id
    would otherwise read as a perfectly good code.
    """
    without_scripts = _SCRIPT_OR_STYLE.sub(" ", markup)
    return html.unescape(_TAG.sub(" ", without_scripts))


def _body_of(text: str | None, markup: str | None) -> str:
    """Prefer the plain-text alternative; fall back to stripped HTML.

    Both would mean scanning the same message twice for no benefit, and the HTML
    part is much the noisier of the two.
    """
    if text and text.strip():
        return text
    return strip_html(markup or "")


def _criteria(
    senders: list[str], *, since: date | None = None, uid_from: int | None = None
) -> str:
    """Build the IMAP search string.

    `uid_from` is the precise form and is preferred wherever a folder has a
    usable cursor. `since` is the fallback for a folder being read for the first
    time: IMAP's date keys have no time component -- that is RFC 3501, not a
    library limit -- so a date is the coarsest possible bound and re-reads
    everything from that day on. UIDs replace it as soon as there is one.
    """
    terms: list[LogicOperator] = []
    if len(senders) > 1:
        # A list passed to one key ANDs the terms, matching nothing. OR is meant.
        terms.append(OR(from_=senders))
    elif senders:
        terms.append(AND(from_=senders[0]))

    if uid_from is not None:
        terms.append(AND(uid=UidRange(str(uid_from), "*")))
    elif since is not None:
        terms.append(AND(date_gte=since))

    if not terms:
        return str(AND(all=True))
    return str(terms[0]) if len(terms) == 1 else str(AND(*terms))


def _folder_status(mailbox: MailBox, folder: str) -> tuple[str | None, int | None]:
    """(UIDVALIDITY, UIDNEXT) for a folder, or (None, None) if it will not say.

    Asked before selecting the folder: RFC 3501 discourages STATUS on the
    currently-selected mailbox, and some servers answer it with stale numbers.
    """
    try:
        status = mailbox.folder.status(folder, ["UIDVALIDITY", "UIDNEXT"])
    except (ImapToolsError, OSError) as exc:
        log.debug("Could not read status of folder %r (%s)", folder, exc)
        return None, None
    uidvalidity = status.get("UIDVALIDITY")
    uidnext = status.get("UIDNEXT")
    return (None if uidvalidity is None else str(uidvalidity)), uidnext


def _window_for(
    folder: str,
    cursor: FolderCursor | None,
    uidvalidity: str | None,
    *,
    since: date | None,
    rescan_since: date | None,
) -> tuple[int | None, date | None]:
    """Decide how to bound one folder's search: (uid_from, since)."""
    if uidvalidity is None:
        # No way to tell whether a stored UID still means anything, so do not
        # trust one. Rare, and costs a re-read rather than a miss.
        return None, since if cursor is None else rescan_since
    if cursor is None:
        return None, since
    if cursor.uidvalidity != uidvalidity:
        log.warning(
            "Folder %r changed UIDVALIDITY (%s -> %s); its position is void, "
            "re-reading from %s.",
            folder,
            cursor.uidvalidity,
            uidvalidity,
            rescan_since.isoformat() if rescan_since else "the beginning",
        )
        return None, rescan_since
    return cursor.uid + 1, None


def _connect(
    *,
    host: str,
    port: int,
    username: str,
    password: str | None,
    access_token: str | None,
    timeout: float,
) -> MailBox:
    try:
        # The constructor opens the socket, so an unresolvable host or a refused
        # connection lands here rather than at login.
        mailbox = MailBox(host, port=port, timeout=timeout)
        if access_token is not None:
            return mailbox.xoauth2(username, access_token)
        if password is None:
            raise MailError(f"Mailbox {username} has no password or token stored.")
        return mailbox.login(username, password)
    except MailboxLoginError as exc:
        hint = (
            "The OAuth2 token was rejected. Re-authorize with `icr mailbox authorize NAME`."
            if access_token is not None
            else "The username or password was rejected. Note that most providers "
            "require an app-specific password for IMAP, not the account password."
        )
        raise MailError(f"{host} rejected the login for {username}. {hint}") from exc
    except (OSError, ImapToolsError) as exc:
        raise MailError(f"Could not reach {host}:{port} -- {exc}") from exc


def scan_mailbox(
    *,
    host: str,
    port: int,
    username: str,
    password: str | None = None,
    access_token: str | None = None,
    senders: list[str],
    cursors: dict[str, FolderCursor] | None = None,
    since: date | None,
    rescan_since: date | None = None,
    limit_per_folder: int,
    timeout: float,
) -> ScanResult:
    """Read every selectable folder and return the messages not yet seen.

    Nothing is marked as read, moved or flagged: this only ever looks. Read
    position is tracked per folder by UID, so a message is fetched and parsed
    once rather than on every poll for as long as it stays inside a date window.
    """
    cursors = dict(cursors or {})
    found: list[FoundMessage] = []

    mailbox = _connect(
        host=host,
        port=port,
        username=username,
        password=password,
        access_token=access_token,
        timeout=timeout,
    )
    try:
        try:
            folders = mailbox.folder.list()
        except ImapToolsError as exc:
            raise MailError(f"Could not list folders on {host}: {exc}") from exc

        selectable = [
            f for f in folders if not {flag.lower() for flag in f.flags} & _UNSELECTABLE
        ]
        log.debug(
            "%s: %d folder(s), %d selectable", username, len(folders), len(selectable)
        )

        for folder in selectable:
            uidvalidity, uidnext = _folder_status(mailbox, folder.name)
            cursor = cursors.get(folder.name)
            uid_from, window = _window_for(
                folder.name, cursor, uidvalidity, since=since, rescan_since=rescan_since
            )
            # Once a folder has been renumbered its old high-water mark is just a
            # number, and carrying it forward would hold the new cursor above
            # UIDs that were never read.
            if uid_from is None:
                cursor = None
            criteria = _criteria(senders, since=window, uid_from=uid_from)

            try:
                mailbox.folder.set(folder.name)
                messages = list(
                    mailbox.fetch(
                        criteria,
                        # Never touch the \Seen flag: this is somebody's real inbox.
                        mark_seen=False,
                        # Oldest first, so that a folder capped by `limit` resumes
                        # from where it stopped on the next poll instead of
                        # stranding everything below the cap forever.
                        reverse=False,
                        limit=limit_per_folder,
                        bulk=True,
                    )
                )
            except ImapToolsError as exc:
                # One awkward folder -- a shared mailbox, a server-side quirk --
                # must not cost us the rest of the account. Its cursor stays put.
                log.debug("%s: skipping folder %r (%s)", username, folder.name, exc)
                continue

            highest = 0
            for message in messages:
                uid = _as_uid(message.uid)
                if uid is None:
                    continue
                # A UID range ending in `*` always includes the folder's highest
                # message, even when the range starts past it. Without this, a
                # folder with nothing new re-delivers its newest mail every poll
                # -- exactly the re-reading the cursor exists to stop.
                if uid_from is not None and uid < uid_from:
                    continue
                highest = max(highest, uid)
                found.append(
                    FoundMessage(
                        folder=folder.name,
                        uid=message.uid or "",
                        subject=message.subject or "",
                        sender=message.from_ or "",
                        date=message.date_str or "",
                        body=_body_of(message.text, message.html),
                    )
                )

            truncated = len(messages) >= limit_per_folder
            if truncated:
                log.info(
                    "%s: folder %r hit the %d-message cap; the rest follows on the "
                    "next poll.",
                    username,
                    folder.name,
                    limit_per_folder,
                )
            if uidvalidity is None:
                # A UID with nothing to qualify it is worse than none at all --
                # it would be trusted after the renumbering it cannot detect.
                continue
            if (advanced := _advance(cursor, highest, uidnext, truncated)) is not None:
                cursors[folder.name] = FolderCursor(uidvalidity=uidvalidity, uid=advanced)
    finally:
        try:
            mailbox.logout()
        except Exception:  # a failed logout has no bearing on what we just read
            log.debug("%s: logout failed", username, exc_info=True)

    return ScanResult(messages=found, cursors=cursors)


def _as_uid(raw: str | None) -> int | None:
    try:
        return int(raw) if raw else None
    except ValueError:  # non-numeric UIDs are not legal, but not worth crashing on
        return None


def _advance(
    cursor: FolderCursor | None, highest: int, uidnext: int | None, truncated: bool
) -> int | None:
    """The folder's new UID position, or None to leave it alone.

    When the read was not capped, everything the search could match has been
    seen, so the position jumps to `UIDNEXT - 1` even if nothing matched --
    otherwise a folder holding no interesting mail never gets a cursor at all and
    re-runs its date scan forever. A capped read only claims what it actually
    read, so the remainder is picked up rather than skipped.
    """
    floor = cursor.uid if cursor else 0
    if truncated:
        return max(floor, highest) or None
    if uidnext is not None:
        return max(floor, highest, uidnext - 1) or None
    return max(floor, highest) or None
