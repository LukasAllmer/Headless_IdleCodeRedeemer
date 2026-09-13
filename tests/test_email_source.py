from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import respx
from imap_tools.errors import ImapToolsError, MailboxLoginError
from imap_tools.folder import FolderInfo

from icr.config import Settings
from icr.db import repo
from icr.mail import imap, oauth
from icr.mail.imap import (
    FolderCursor,
    FoundMessage,
    MailError,
    ScanResult,
    _body_of,
    _criteria,
    strip_html,
)
from icr.models import MailboxAuth
from icr.sources import email_inbox
from icr.sources.base import extract_codes, poll_source

SENDER = "newsletters@codenameentertainment.com"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(db_path=tmp_path / "t.sqlite3", email_enabled=True)


@pytest.fixture
def account(conn: sqlite3.Connection):
    return repo.add_account(conn, name="main", user_id="u1", user_hash="h1")


def add_mailbox(conn: sqlite3.Connection, account_id: int, **overrides):
    kwargs = {
        "name": "main-mail",
        "account_id": account_id,
        "auth": MailboxAuth.PASSWORD,
        "host": "imap.example.com",
        "port": 993,
        "username": "player@example.com",
        "secret": "app-password",
    }
    kwargs.update(overrides)
    return repo.add_mailbox(conn, **kwargs)


def message(body: str, *, folder: str = "INBOX", uid: str = "1", subject: str = "Your code"):
    return FoundMessage(
        folder=folder,
        uid=uid,
        subject=subject,
        sender=SENDER,
        date="Mon, 08 Sep 2026 09:00:00 +0000",
        body=body,
    )


def stub_scan(monkeypatch, messages, *, spy: dict | None = None, cursors=None):
    """Replace the blocking IMAP scan. `read_mailbox` hands it to
    `asyncio.to_thread`, so it stays an ordinary synchronous function."""

    def fake(**kwargs):
        if spy is not None:
            spy.update(kwargs)
        if isinstance(messages, Exception):
            raise messages
        listed = list(messages)
        return ScanResult(
            messages=listed,
            cursors=cursors
            if cursors is not None
            else {m.folder: FolderCursor("1", int(m.uid)) for m in listed},
        )

    monkeypatch.setattr(email_inbox, "scan_mailbox", fake)


def stored_cursors(conn: sqlite3.Connection, mailbox_id: int) -> dict:
    raw = repo.kv_get(conn, email_inbox.cursor_key(mailbox_id))
    return json.loads(raw)["folders"] if raw else {}


# --------------------------------------------------------------------------
# body handling
# --------------------------------------------------------------------------


def test_strip_html_puts_a_gap_where_the_tag_was() -> None:
    """Dropping tags outright would weld adjacent cells into a twelve-character
    run that reads as a perfectly good code."""
    assert extract_codes(strip_html("<td>ABCDEF</td><td>GHIJKL</td>")) == []
    assert extract_codes(strip_html("<td>ABCDEFGHIJKL</td>")) == ["ABCDEFGHIJKL"]


def test_strip_html_drops_script_bodies() -> None:
    assert "var" not in strip_html("<script>var ABCDEFGHIJKL = 1;</script>hello")


def test_strip_html_unescapes_entities() -> None:
    assert strip_html("<p>a &amp; b</p>").strip() == "a & b"


def test_plain_text_wins_over_html() -> None:
    """The HTML part is the same message plus tracking noise, and `extract_codes`
    upper-cases before matching, so a lowercase hex token in a URL would read as
    a code."""
    assert _body_of("the real body", "<p>ABCDEFGHIJKL</p>") == "the real body"


def test_html_is_used_when_there_is_no_text_part() -> None:
    assert "ABCDEFGHIJKL" in _body_of("   ", "<p>ABCDEFGHIJKL</p>")


# --------------------------------------------------------------------------
# search criteria
# --------------------------------------------------------------------------


def test_criteria_filters_by_sender_and_date() -> None:
    built = _criteria([SENDER], since=date(2026, 9, 1))
    assert f'FROM "{SENDER}"' in built
    assert "SINCE 1-Sep-2026" in built


def test_criteria_ors_multiple_senders() -> None:
    """A list handed to one key ANDs the terms, which matches nothing."""
    assert "OR" in _criteria(["a@x.com", "b@y.com"])


def test_criteria_without_a_filter_is_everything() -> None:
    assert _criteria([]) == "(ALL)"


def test_criteria_asks_for_a_uid_range_when_there_is_a_position() -> None:
    """The whole point of the UID cursor: the server is asked for what came
    after the last message read, not for a day's worth of mail to re-filter."""
    built = _criteria([SENDER], uid_from=42)
    assert "UID 42:*" in built
    assert "SINCE" not in built


def test_a_uid_range_wins_over_a_date() -> None:
    """Both bounds together would be an AND, and the date is the loose one."""
    assert "SINCE" not in _criteria([], since=date(2026, 9, 1), uid_from=42)


# --------------------------------------------------------------------------
# polling
# --------------------------------------------------------------------------


async def test_codes_are_bound_to_the_mailbox_account(
    conn: sqlite3.Connection, settings: Settings, account, monkeypatch
) -> None:
    other = repo.add_account(conn, name="alt", user_id="u2", user_hash="h2")
    add_mailbox(conn, account.id)
    stub_scan(monkeypatch, [message("Your code is ABCDEFGHIJKL")])

    result = await poll_source(email_inbox.EmailSource(), conn, settings)

    assert (result.found, result.added) == (1, 1)
    stored = repo.get_code(conn, "ABCDEFGHIJKL")
    assert stored is not None and stored.account_id == account.id

    work = {(w.account_name, w.code) for w in repo.outstanding_work(conn, max_attempts=3)}
    assert work == {("main", "ABCDEFGHIJKL")}, f"must never be offered to {other.name}"


async def test_source_ref_and_note_identify_the_message(
    conn: sqlite3.Connection, settings: Settings, account, monkeypatch
) -> None:
    add_mailbox(conn, account.id)
    stub_scan(monkeypatch, [message("ABCDEFGHIJKL", folder="Archive", uid="77")])

    await poll_source(email_inbox.EmailSource(), conn, settings)

    stored = repo.get_code(conn, "ABCDEFGHIJKL")
    assert stored is not None
    assert stored.source_ref == "main-mail:Archive:77"
    assert stored.note is not None and "player@example.com" in stored.note


async def test_the_subject_is_scanned_too(
    conn: sqlite3.Connection, settings: Settings, account, monkeypatch
) -> None:
    add_mailbox(conn, account.id)
    stub_scan(monkeypatch, [message("nothing here", subject="Code ABCDEFGHIJKL inside")])

    result = await poll_source(email_inbox.EmailSource(), conn, settings)

    assert result.added == 1


async def test_first_poll_reads_the_whole_mailbox(
    conn: sqlite3.Connection, settings: Settings, account, monkeypatch
) -> None:
    """'The complete e-mail account should be looked through' -- so the opening
    scan has no lower bound."""
    add_mailbox(conn, account.id)
    spy: dict = {}
    stub_scan(monkeypatch, [], spy=spy)

    await poll_source(email_inbox.EmailSource(), conn, settings)

    assert spy["since"] is None
    assert spy["senders"] == [SENDER]


async def test_uid_positions_are_persisted_per_folder(
    conn: sqlite3.Connection, settings: Settings, account, monkeypatch
) -> None:
    mailbox = add_mailbox(conn, account.id)
    stub_scan(
        monkeypatch,
        [message("ABCDEFGHIJKL", folder="INBOX", uid="12")],
        cursors={"INBOX": FolderCursor("7", 12), "Archive": FolderCursor("7", 3)},
    )

    await poll_source(email_inbox.EmailSource(), conn, settings)

    assert stored_cursors(conn, mailbox.id) == {
        "Archive": {"uidvalidity": "7", "uid": 3},
        "INBOX": {"uidvalidity": "7", "uid": 12},
    }


async def test_a_stored_position_is_handed_back_to_the_next_scan(
    conn: sqlite3.Connection, settings: Settings, account, monkeypatch
) -> None:
    """The cursor is what stops the same message being fetched and parsed every
    five minutes, so it has to survive the round trip through the database."""
    mailbox = add_mailbox(conn, account.id)
    repo.kv_set(
        conn,
        email_inbox.cursor_key(mailbox.id),
        json.dumps({"folders": {"INBOX": {"uidvalidity": "7", "uid": 12}}}),
    )
    spy: dict = {}
    stub_scan(monkeypatch, [], spy=spy, cursors={"INBOX": FolderCursor("7", 12)})

    await poll_source(email_inbox.EmailSource(), conn, settings)

    assert spy["cursors"] == {"INBOX": FolderCursor("7", 12)}
    assert spy["since"] is None


async def test_a_date_cursor_from_before_the_uid_change_still_works(
    conn: sqlite3.Connection, settings: Settings, account, monkeypatch
) -> None:
    """Upgrading must not re-read the whole mailbox: the old date becomes the
    opening window, and UID positions take over from there."""
    mailbox = add_mailbox(conn, account.id)
    repo.kv_set(conn, email_inbox.cursor_key(mailbox.id), "2026-09-08")
    spy: dict = {}
    stub_scan(monkeypatch, [], spy=spy, cursors={"INBOX": FolderCursor("7", 40)})

    await poll_source(email_inbox.EmailSource(), conn, settings)

    assert spy["cursors"] == {}
    assert spy["since"] == date(2026, 9, 8) - timedelta(days=settings.email_rescan_days)
    assert stored_cursors(conn, mailbox.id) == {"INBOX": {"uidvalidity": "7", "uid": 40}}


async def test_an_unreadable_cursor_rescans_rather_than_crashing(
    conn: sqlite3.Connection, settings: Settings, account, monkeypatch
) -> None:
    mailbox = add_mailbox(conn, account.id)
    repo.kv_set(conn, email_inbox.cursor_key(mailbox.id), "not-a-date")
    spy: dict = {}
    stub_scan(monkeypatch, [], spy=spy)

    result = await poll_source(email_inbox.EmailSource(), conn, settings)

    assert result.error is None
    assert spy["since"] is None
    assert spy["cursors"] == {}


async def test_a_corrupt_folder_entry_is_dropped_not_fatal(
    conn: sqlite3.Connection, settings: Settings, account, monkeypatch
) -> None:
    mailbox = add_mailbox(conn, account.id)
    repo.kv_set(
        conn,
        email_inbox.cursor_key(mailbox.id),
        json.dumps(
            {
                "folders": {
                    "INBOX": {"uid": "not-a-number"},
                    "Archive": {"uidvalidity": "7", "uid": 3},
                }
            }
        ),
    )
    spy: dict = {}
    stub_scan(monkeypatch, [], spy=spy)

    result = await poll_source(email_inbox.EmailSource(), conn, settings)

    assert result.error is None
    assert spy["cursors"] == {"Archive": FolderCursor("7", 3)}


async def test_a_uidvalidity_break_is_bounded_by_the_rescan_window(
    conn: sqlite3.Connection, settings: Settings, account, monkeypatch
) -> None:
    """The codes are already on file, so a folder that lost its numbering only
    needs recent mail re-read, not its whole history."""
    mailbox = add_mailbox(conn, account.id)
    stub_scan(monkeypatch, [], spy=(spy := {}))

    await poll_source(email_inbox.EmailSource(), conn, settings)

    assert spy["rescan_since"] == datetime.now(UTC).date() - timedelta(
        days=settings.email_rescan_days
    )
    assert mailbox.id  # the fixture is the subject, not incidental


async def test_reset_makes_the_next_poll_start_over(
    conn: sqlite3.Connection, settings: Settings, account, monkeypatch
) -> None:
    mailbox = add_mailbox(conn, account.id)
    stub_scan(monkeypatch, [message("ABCDEFGHIJKL")])
    source = email_inbox.EmailSource()

    await poll_source(source, conn, settings)
    assert source.position(conn) is not None

    source.reset(conn)

    assert source.position(conn) is None
    assert repo.kv_get(conn, email_inbox.cursor_key(mailbox.id)) is None


def test_position_reports_folders_and_the_highest_uid(conn: sqlite3.Connection, account) -> None:
    mailbox = add_mailbox(conn, account.id)
    repo.kv_set(
        conn,
        email_inbox.cursor_key(mailbox.id),
        json.dumps(
            {
                "folders": {
                    "INBOX": {"uidvalidity": "7", "uid": 12},
                    "Archive": {"uidvalidity": "7", "uid": 40},
                }
            }
        ),
    )

    position = email_inbox.EmailSource().position(conn)

    assert position is not None
    assert "main-mail=2 folder(s), highest uid 40" in position


# --------------------------------------------------------------------------
# the IMAP scan itself, against a stand-in server
# --------------------------------------------------------------------------


class FakeFolders:
    def __init__(self, folders: list[FolderInfo]) -> None:
        self._folders = folders
        self.selected: str | None = None
        self.uidvalidity = 7
        self.uidnext = 10

    def list(self) -> list[FolderInfo]:
        return self._folders

    def set(self, name: str) -> None:
        if name == "Broken":
            raise ImapToolsError("this folder cannot be selected")
        self.selected = name

    def status(self, name: str, options=None) -> dict[str, int]:
        if name == "Silent":
            raise ImapToolsError("this server will not answer STATUS")
        return {"UIDVALIDITY": self.uidvalidity, "UIDNEXT": self.uidnext}


class FakeMailBox:
    """Stands in for a server. Records what it was asked to do."""

    last: FakeMailBox | None = None

    def __init__(self, host: str, port: int = 993, timeout: float | None = None) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.folder = FakeFolders(
            [
                FolderInfo("INBOX", "/", ()),
                FolderInfo("Broken", "/", ()),
                # Deliberately after the broken one: a folder that cannot be
                # selected must not cost us the rest of the account.
                FolderInfo("Archive", "/", ()),
                FolderInfo("[Gmail]", "/", ("\\Noselect",)),
            ]
        )
        self.credentials: tuple[str, str, str] | None = None
        self.fetch_calls: list[dict] = []
        self.logged_out = False
        FakeMailBox.last = self

    def login(self, username: str, password: str) -> FakeMailBox:
        self.credentials = ("password", username, password)
        return self

    def xoauth2(self, username: str, access_token: str) -> FakeMailBox:
        self.credentials = ("xoauth2", username, access_token)
        return self

    #: UIDs the next fetch will return, whatever the criteria.
    returns = ("9",)

    def fetch(self, criteria: str, **kwargs: object):
        self.fetch_calls.append({"folder": self.folder.selected, "criteria": criteria, **kwargs})
        return iter(
            [
                SimpleNamespace(
                    uid=uid,
                    subject="Your code",
                    from_=SENDER,
                    date_str="Mon, 08 Sep 2026 09:00:00 +0000",
                    text=f"code ABCDEFGHIJKL in {self.folder.selected}",
                    html=None,
                )
                for uid in self.returns
            ]
        )

    def logout(self) -> None:
        self.logged_out = True


def run_scan(monkeypatch, *, box=FakeMailBox, **overrides) -> ScanResult:
    monkeypatch.setattr(imap, "MailBox", box)
    kwargs = {
        "host": "imap.example.com",
        "port": 993,
        "username": "player@example.com",
        "password": "app-password",
        "senders": [SENDER],
        "since": None,
        "limit_per_folder": 200,
        "timeout": 60.0,
    }
    kwargs.update(overrides)
    return imap.scan_mailbox(**kwargs)


def test_scan_never_marks_a_message_as_read(monkeypatch) -> None:
    """This runs against somebody's real inbox. Reading it must not change it."""
    run_scan(monkeypatch)

    assert FakeMailBox.last is not None
    assert FakeMailBox.last.fetch_calls
    assert all(call["mark_seen"] is False for call in FakeMailBox.last.fetch_calls)


def test_scan_covers_every_selectable_folder(monkeypatch) -> None:
    """'The complete e-mail account (all folders) should be looked through' --
    people file the newsletter away, or their provider does it for them."""
    result = run_scan(monkeypatch)

    assert {m.folder for m in result.messages} == {"INBOX", "Archive"}


def test_an_unselectable_folder_is_skipped_not_fatal(monkeypatch) -> None:
    result = run_scan(monkeypatch)

    assert [m.folder for m in result.messages].count("Broken") == 0
    assert len(result.messages) == 2, "the folders after the broken one still get read"


# --------------------------------------------------------------------------
# UID cursors
# --------------------------------------------------------------------------


def test_a_scan_reads_oldest_first_so_a_capped_folder_can_resume(monkeypatch) -> None:
    """Newest-first plus a limit would strand everything below the cap forever;
    the cursor only advances through what was actually read."""
    run_scan(monkeypatch)

    assert FakeMailBox.last is not None
    assert all(call["reverse"] is False for call in FakeMailBox.last.fetch_calls)


def test_the_second_scan_asks_only_for_what_came_after_the_first(monkeypatch) -> None:
    first = run_scan(monkeypatch)

    run_scan(monkeypatch, cursors=first.cursors)

    assert FakeMailBox.last is not None
    assert all("UID" in call["criteria"] for call in FakeMailBox.last.fetch_calls)


def test_the_highest_uid_is_remembered_per_folder(monkeypatch) -> None:
    result = run_scan(monkeypatch)

    # UIDNEXT is 10, so everything currently in the folder has been seen.
    assert result.cursors["INBOX"] == FolderCursor(uidvalidity="7", uid=9)
    assert result.cursors["Archive"] == FolderCursor(uidvalidity="7", uid=9)


def test_a_folder_with_no_matching_mail_still_gets_a_position(monkeypatch) -> None:
    """Otherwise a folder holding nothing interesting re-runs its date scan on
    every poll and never settles."""
    monkeypatch.setattr(FakeMailBox, "returns", ())

    result = run_scan(monkeypatch)

    assert result.cursors["INBOX"] == FolderCursor(uidvalidity="7", uid=9)


def test_the_trailing_star_message_is_filtered_out(monkeypatch) -> None:
    """A UID range ending in `*` always includes the folder's newest message,
    even when the range starts past it. Trusting the server here is what made
    the same mail get re-read on every poll."""
    result = run_scan(monkeypatch, cursors={"INBOX": FolderCursor("7", 9)})

    assert [m.folder for m in result.messages] == ["Archive"], "INBOX had nothing new"
    assert result.cursors["INBOX"] == FolderCursor("7", 9)


def test_a_uidvalidity_change_voids_the_position(monkeypatch) -> None:
    result = run_scan(
        monkeypatch,
        cursors={"INBOX": FolderCursor("OLD", 9)},
        rescan_since=date(2026, 9, 1),
    )

    assert FakeMailBox.last is not None
    inbox = next(c for c in FakeMailBox.last.fetch_calls if c["folder"] == "INBOX")
    assert "SINCE 1-Sep-2026" in inbox["criteria"], "the stale UID must not be used"
    assert "UID" not in inbox["criteria"]
    assert result.cursors["INBOX"] == FolderCursor("7", 9), "renumbered, then re-anchored"


def test_a_capped_folder_claims_only_what_it_read(monkeypatch) -> None:
    """UIDNEXT would say the whole folder is done, but the read stopped at the
    limit -- so the remainder has to stay outstanding."""
    monkeypatch.setattr(FakeMailBox, "returns", ("4", "5"))

    result = run_scan(monkeypatch, limit_per_folder=2)

    assert result.cursors["INBOX"] == FolderCursor("7", 5), "not UIDNEXT-1 of 9"


def test_a_folder_that_will_not_report_status_falls_back_to_a_date(monkeypatch) -> None:
    """Without UIDVALIDITY there is nothing to qualify a UID with, and a cursor
    that cannot notice renumbering would skip mail rather than re-read it."""

    class SilentStatus(FakeMailBox):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]
            self.folder = FakeFolders([FolderInfo("Silent", "/", ())])

    result = run_scan(monkeypatch, box=SilentStatus, since=date(2026, 9, 1))

    assert FakeMailBox.last is not None
    assert "SINCE 1-Sep-2026" in FakeMailBox.last.fetch_calls[0]["criteria"]
    assert result.cursors == {}, "an unverifiable position is not stored"


def test_an_unselectable_folder_keeps_its_position(monkeypatch) -> None:
    """A folder that is briefly awkward should resume, not rescan."""
    result = run_scan(monkeypatch, cursors={"Broken": FolderCursor("7", 5)})

    assert result.cursors["Broken"] == FolderCursor("7", 5)


def test_scan_logs_out_even_when_a_folder_explodes(monkeypatch) -> None:
    run_scan(monkeypatch)

    assert FakeMailBox.last is not None
    assert FakeMailBox.last.logged_out


def test_scan_uses_oauth2_when_given_a_token(monkeypatch) -> None:
    run_scan(monkeypatch, password=None, access_token="token-value")

    assert FakeMailBox.last is not None
    assert FakeMailBox.last.credentials == ("xoauth2", "player@example.com", "token-value")


def test_an_unreachable_host_is_an_ordinary_failure_not_a_crash(monkeypatch) -> None:
    """`MailBox()` opens the socket in its constructor, so a host that does not
    resolve fails before login. Missing that turned a typo'd hostname into a
    full traceback in the log instead of one line naming the host."""

    def explode(*args: object, **kwargs: object):
        raise OSError("[Errno -5] No address associated with hostname")

    monkeypatch.setattr(imap, "MailBox", explode)

    with pytest.raises(MailError, match=re.escape("Could not reach imap.example.com:993")):
        imap.scan_mailbox(
            host="imap.example.com",
            port=993,
            username="player@example.com",
            password="app-password",
            senders=[SENDER],
            since=None,
            limit_per_folder=200,
            timeout=60.0,
        )


def test_a_rejected_login_names_the_app_password_trap(monkeypatch) -> None:
    class Rejecting(FakeMailBox):
        def login(self, username: str, password: str) -> FakeMailBox:
            raise MailboxLoginError(None, b"AUTHENTICATIONFAILED")

    monkeypatch.setattr(imap, "MailBox", Rejecting)

    with pytest.raises(MailError, match="app-specific password"):
        imap.scan_mailbox(
            host="imap.example.com",
            port=993,
            username="player@example.com",
            password="wrong",
            senders=[SENDER],
            since=None,
            limit_per_folder=200,
            timeout=60.0,
        )


# --------------------------------------------------------------------------
# failure handling
# --------------------------------------------------------------------------


async def test_no_mailboxes_is_reported_as_misconfiguration(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    result = await poll_source(email_inbox.EmailSource(), conn, settings)

    assert result.error is not None
    assert "icr mailbox add" in result.error


async def test_one_bad_mailbox_does_not_stop_the_others(
    conn: sqlite3.Connection, settings: Settings, account, monkeypatch
) -> None:
    other = repo.add_account(conn, name="alt", user_id="u2", user_hash="h2")
    good = add_mailbox(conn, account.id, name="good")
    bad = add_mailbox(conn, other.id, name="bad", username="broken@example.com")

    def fake(**kwargs):
        if kwargs["username"] == "broken@example.com":
            raise MailError("the password was rejected")
        return ScanResult(messages=[message("ABCDEFGHIJKL")], cursors={})

    monkeypatch.setattr(email_inbox, "scan_mailbox", fake)

    result = await poll_source(email_inbox.EmailSource(), conn, settings)

    assert result.error is None
    assert result.added == 1
    assert repo.require_mailbox(conn, good.name).last_error is None
    assert "rejected" in (repo.require_mailbox(conn, bad.name).last_error or "")


async def test_every_mailbox_failing_fails_the_source(
    conn: sqlite3.Connection, settings: Settings, account, monkeypatch
) -> None:
    add_mailbox(conn, account.id)
    stub_scan(monkeypatch, MailError("cannot reach imap.example.com"))

    result = await poll_source(email_inbox.EmailSource(), conn, settings)

    assert result.error is not None
    assert "cannot reach" in result.error


async def test_a_failed_poll_does_not_advance_the_cursor(
    conn: sqlite3.Connection, settings: Settings, account, monkeypatch
) -> None:
    mailbox = add_mailbox(conn, account.id)
    stub_scan(monkeypatch, MailError("nope"))

    await poll_source(email_inbox.EmailSource(), conn, settings)

    assert repo.kv_get(conn, email_inbox.cursor_key(mailbox.id)) is None


async def test_a_disabled_mailbox_is_skipped(
    conn: sqlite3.Connection, settings: Settings, account, monkeypatch
) -> None:
    add_mailbox(conn, account.id)
    repo.set_mailbox_enabled(conn, "main-mail", False)
    stub_scan(monkeypatch, [message("ABCDEFGHIJKL")])

    result = await poll_source(email_inbox.EmailSource(), conn, settings)

    assert result.error is not None
    assert result.added == 0


async def test_an_unauthorized_microsoft_mailbox_says_what_to_run(
    conn: sqlite3.Connection, settings: Settings, account
) -> None:
    add_mailbox(conn, account.id, auth=MailboxAuth.MICROSOFT, secret=None)

    result = await poll_source(email_inbox.EmailSource(), conn, settings)

    assert result.error is not None
    assert "icr mailbox authorize" in result.error


# --------------------------------------------------------------------------
# microsoft oauth2
# --------------------------------------------------------------------------

TOKEN_URL = f"{oauth.AUTHORITY}/common/oauth2/v2.0/token"
DEVICE_URL = f"{oauth.AUTHORITY}/common/oauth2/v2.0/devicecode"


@respx.mock
async def test_a_rotated_refresh_token_is_written_back(
    conn: sqlite3.Connection, settings: Settings, account, monkeypatch
) -> None:
    """Microsoft rotates these. Keeping the old one works right up until it is
    invalidated, and then the mailbox dies for no visible reason."""
    mailbox = add_mailbox(
        conn,
        account.id,
        auth=MailboxAuth.MICROSOFT,
        secret="old-refresh-token",
        oauth_client_id="client-123",
    )
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "fresh-access",
                "refresh_token": "new-refresh-token",
                "expires_in": 3600,
            },
        )
    )
    spy: dict = {}
    stub_scan(monkeypatch, [], spy=spy)

    await poll_source(email_inbox.EmailSource(), conn, settings)

    assert spy["access_token"] == "fresh-access"
    assert spy["password"] is None
    assert repo.require_mailbox(conn, mailbox.name).secret == "new-refresh-token"


@respx.mock
async def test_a_dead_refresh_token_points_at_the_fix(
    conn: sqlite3.Connection, settings: Settings, account
) -> None:
    add_mailbox(
        conn,
        account.id,
        auth=MailboxAuth.MICROSOFT,
        secret="stale",
        oauth_client_id="client-123",
    )
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": "AADSTS700082: The refresh token has expired.",
            },
        )
    )

    result = await poll_source(email_inbox.EmailSource(), conn, settings)

    assert result.error is not None
    assert "AADSTS700082" in result.error
    assert "icr mailbox authorize" in result.error


@respx.mock
async def test_device_code_flow_waits_for_approval() -> None:
    respx.post(DEVICE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "device_code": "dev-code",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://microsoft.com/devicelogin",
                "expires_in": 30,
                "interval": 0,  # keeps the test from actually sleeping
            },
        )
    )
    respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(400, json={"error": "authorization_pending"}),
            httpx.Response(
                200, json={"access_token": "a", "refresh_token": "r", "expires_in": 3600}
            ),
        ]
    )

    prompt = await oauth.begin_device_code(client_id="client-123")
    assert "ABCD-EFGH" in prompt.describe()

    tokens = await oauth.poll_device_code(prompt, client_id="client-123")

    assert tokens.refresh_token == "r"


@respx.mock
async def test_a_declined_sign_in_stops_polling() -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "authorization_declined"})
    )
    prompt = oauth.DeviceCodePrompt(
        device_code="d",
        user_code="U",
        verification_uri="https://microsoft.com/devicelogin",
        expires_in=30,
        interval=0,
    )

    with pytest.raises(oauth.OAuthError, match="declined"):
        await oauth.poll_device_code(prompt, client_id="client-123")


@respx.mock
async def test_a_public_client_misconfiguration_is_named() -> None:
    respx.post(DEVICE_URL).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": "invalid_client",
                "error_description": "AADSTS7000218: request body must contain client_assertion.",
            },
        )
    )

    with pytest.raises(oauth.OAuthError, match="public"):
        await oauth.begin_device_code(client_id="client-123")
