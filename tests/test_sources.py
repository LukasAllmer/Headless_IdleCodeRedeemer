from __future__ import annotations

import sqlite3

import httpx
import pytest
import respx

from icr.config import Settings
from icr.db import repo
from icr.sources.base import extract_codes, poll_source
from icr.sources.discord_follow import API_BASE, KV_CURSOR, DiscordFollowSource

CHANNEL = "999888777"
MESSAGES_URL = f"{API_BASE}/channels/{CHANNEL}/messages"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        db_path=tmp_path / "t.sqlite3",
        discord_enabled=True,
        discord_bot_token="bot-token-value",
        discord_mirror_channel_id=CHANNEL,
    )


def message(mid: str, content: str = "", **extra: object) -> dict:
    return {
        "id": mid,
        "content": content,
        "author": {"username": "IdleChampions"},
        "timestamp": "2026-09-04T10:00:00+00:00",
        **extra,
    }


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------


def test_extracts_plain_code() -> None:
    assert extract_codes("New code: ABCDEFGHIJKL enjoy!") == ["ABCDEFGHIJKL"]


def test_strips_dashes_and_uppercases() -> None:
    assert extract_codes("abcd-efgh-ijkl") == ["ABCDEFGHIJKL"]


def test_extracts_sixteen_character_code() -> None:
    assert extract_codes("ABCDEFGHIJKLMNOP") == ["ABCDEFGHIJKLMNOP"]


def test_extracts_multiple_codes_from_one_message() -> None:
    """The extension took only the first match per message; this takes all."""
    text = "Codes: ABCDEFGHIJKL and QRSTUVWXYZ12 are live"
    assert extract_codes(text) == ["ABCDEFGHIJKL", "QRSTUVWXYZ12"]


def test_deduplicates_within_a_message() -> None:
    assert extract_codes("ABCDEFGHIJKL ABCDEFGHIJKL") == ["ABCDEFGHIJKL"]


def test_ignores_short_strings() -> None:
    assert extract_codes("too short ABCDEF and hello world") == []


# --------------------------------------------------------------------------
# discord source
# --------------------------------------------------------------------------


@respx.mock
async def test_poll_finds_codes_and_advances_cursor(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    respx.get(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200,
            json=[  # newest first, as Discord returns them
                message("200", "Second code: QRSTUVWXYZ12"),
                message("100", "First code: ABCDEFGHIJKL"),
            ],
        )
    )

    result = await poll_source(DiscordFollowSource(), conn, settings)

    assert result.found == 2
    assert result.added == 2
    assert repo.kv_get(conn, KV_CURSOR) == "200"
    stored = {c.code: c for c in repo.list_codes(conn)}
    assert stored["ABCDEFGHIJKL"].source == "discord"
    assert stored["ABCDEFGHIJKL"].source_ref == "100"


@respx.mock
async def test_poll_sends_cursor_as_after(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    repo.kv_set(conn, KV_CURSOR, "150")
    route = respx.get(MESSAGES_URL).mock(return_value=httpx.Response(200, json=[]))

    await poll_source(DiscordFollowSource(), conn, settings)

    assert route.calls.last.request.url.params["after"] == "150"


@respx.mock
async def test_poll_reads_embeds(conn: sqlite3.Connection, settings: Settings) -> None:
    respx.get(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                message(
                    "300",
                    content="",
                    embeds=[
                        {
                            "title": "Weekly combination",
                            "description": "Use ABCDEFGHIJKL before Friday",
                            "fields": [{"name": "Bonus", "value": "QRSTUVWXYZ12"}],
                        }
                    ],
                )
            ],
        )
    )

    result = await poll_source(DiscordFollowSource(), conn, settings)

    assert result.found == 2


@respx.mock
async def test_duplicate_codes_are_not_re_added(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    repo.add_code(conn, "ABCDEFGHIJKL", source="manual")
    respx.get(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=[message("100", "ABCDEFGHIJKL")])
    )

    result = await poll_source(DiscordFollowSource(), conn, settings)

    assert result.found == 1
    assert result.added == 0


@respx.mock
async def test_cursor_not_advanced_when_no_messages(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    repo.kv_set(conn, KV_CURSOR, "150")
    respx.get(MESSAGES_URL).mock(return_value=httpx.Response(200, json=[]))

    await poll_source(DiscordFollowSource(), conn, settings)

    assert repo.kv_get(conn, KV_CURSOR) == "150"


@respx.mock
async def test_unauthorized_is_reported_not_raised(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """A broken source must not take the service down."""
    respx.get(MESSAGES_URL).mock(return_value=httpx.Response(401, json={}))

    result = await poll_source(DiscordFollowSource(), conn, settings)

    assert result.error is not None
    assert "token" in result.error.lower()
    assert result.added == 0


@respx.mock
async def test_forbidden_names_the_missing_permissions(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    respx.get(MESSAGES_URL).mock(return_value=httpx.Response(403, json={}))

    result = await poll_source(DiscordFollowSource(), conn, settings)

    assert result.error is not None
    assert "Read Message History" in result.error


@respx.mock
async def test_config_errors_are_logged_without_a_traceback(
    conn: sqlite3.Connection, settings: Settings, caplog
) -> None:
    """A bad token is a misconfiguration, not a crash -- the message is the whole
    story, and a stack trace just buries it."""
    respx.get(MESSAGES_URL).mock(return_value=httpx.Response(401, json={}))

    with caplog.at_level("ERROR"):
        await poll_source(DiscordFollowSource(), conn, settings)

    record = next(r for r in caplog.records if r.name.endswith("sources.base"))
    assert record.exc_info is None
    assert "ICR_DISCORD_BOT_TOKEN" in record.getMessage()


@respx.mock
async def test_unexpected_errors_keep_their_traceback(
    conn: sqlite3.Connection, settings: Settings, caplog
) -> None:
    respx.get(MESSAGES_URL).mock(side_effect=httpx.ConnectError("no route to host"))

    with caplog.at_level("ERROR"):
        result = await poll_source(DiscordFollowSource(), conn, settings)

    record = next(r for r in caplog.records if r.name.endswith("sources.base"))
    assert record.exc_info is not None
    assert result.error is not None


@respx.mock
async def test_rate_limit_is_retried(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    respx.get(MESSAGES_URL).mock(
        side_effect=[
            httpx.Response(429, json={"retry_after": 0.01}),
            httpx.Response(200, json=[message("100", "ABCDEFGHIJKL")]),
        ]
    )

    result = await poll_source(DiscordFollowSource(), conn, settings)

    assert result.added == 1
