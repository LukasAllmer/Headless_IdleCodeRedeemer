"""The two public code lists.

Fixtures are trimmed captures of the real pages, so a layout change on either
site shows up here as a failing test rather than as a silent zero.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest
import respx

from icr.config import Settings
from icr.db import repo
from icr.sources import fandom_wiki, incendar
from icr.sources.base import poll_source
from icr.sources.web import USER_AGENT

FIXTURES = Path(__file__).parent / "fixtures"
INCENDAR_HTML = (FIXTURES / "incendar_codes.html").read_text(encoding="utf-8")
FANDOM_JSON = (FIXTURES / "fandom_combinations.json").read_text(encoding="utf-8")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(db_path=tmp_path / "t.sqlite3")


def incendar_route():
    return respx.get(incendar.URL)


def fandom_route():
    return respx.get(fandom_wiki.API)


# --------------------------------------------------------------------------
# incendar
# --------------------------------------------------------------------------


@respx.mock
async def test_incendar_finds_the_codes(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    incendar_route().mock(return_value=httpx.Response(200, text=INCENDAR_HTML))

    result = await poll_source(incendar.IncendarSource(), conn, settings)

    assert result.error is None
    assert result.found == 6
    assert result.added == 6
    codes = {c.code for c in repo.list_codes(conn)}
    assert "MINIBEDSSIPE" in codes


@respx.mock
async def test_incendar_codes_are_public(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """These lists are multi-use codes -- every account may redeem them."""
    repo.add_account(conn, name="main", user_id="u1", user_hash="h1")
    repo.add_account(conn, name="alt", user_id="u2", user_hash="h2")
    incendar_route().mock(return_value=httpx.Response(200, text=INCENDAR_HTML))

    await poll_source(incendar.IncendarSource(), conn, settings)

    assert all(c.account_id is None for c in repo.list_codes(conn))
    work = repo.outstanding_work(conn, max_attempts=3)
    assert len({w.account_name for w in work}) == 2


@respx.mock
async def test_incendar_keeps_the_reward_text_as_a_note(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    incendar_route().mock(return_value=httpx.Response(200, text=INCENDAR_HTML))

    await poll_source(incendar.IncendarSource(), conn, settings)

    stored = repo.get_code(conn, "MINIBEDSSIPE")
    assert stored is not None
    assert stored.note is not None and "Chest" in stored.note
    assert stored.source_ref is not None and stored.source_ref.startswith("incendar:")


@respx.mock
async def test_incendar_layout_change_is_reported(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """The codes live in input attributes, so a redesign yields nothing at all.
    Silently finding zero codes forever is the failure worth catching."""
    incendar_route().mock(
        return_value=httpx.Response(200, text="<html><body>Under maintenance</body></html>")
    )

    result = await poll_source(incendar.IncendarSource(), conn, settings)

    assert result.error is not None
    assert "layout" in result.error


@respx.mock
async def test_incendar_sends_a_user_agent_that_says_who_it_is(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """It is a small community site. Polling it anonymously every few minutes
    with no way to identify the client is rude."""
    route = incendar_route().mock(return_value=httpx.Response(200, text=INCENDAR_HTML))

    await poll_source(incendar.IncendarSource(), conn, settings)

    assert route.calls.last.request.headers["User-Agent"] == USER_AGENT


# --------------------------------------------------------------------------
# fandom
# --------------------------------------------------------------------------


@respx.mock
async def test_fandom_finds_the_codes(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    fandom_route().mock(return_value=httpx.Response(200, text=FANDOM_JSON))

    result = await poll_source(fandom_wiki.FandomWikiSource(), conn, settings)

    assert result.error is None
    assert result.added == 5
    assert "CARAMONPARTY" in {c.code for c in repo.list_codes(conn)}


@respx.mock
async def test_fandom_ignores_the_account_restricted_code_in_prose(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """Reading only the {{combination}} templates, rather than the whole page,
    is what keeps this parser precise: the prose sections carry ordinary English,
    external links, and -- as here -- a bare code for the Crusaders of the Lost
    Idols sunsetting that only works for accounts with a CotLI history.

    Incendar happens to list that same code in its table, so it reaches the queue
    anyway. That is not a reason to be sloppy here; it is a reason the wiki's own
    curation (the table) is the part worth trusting."""
    fandom_route().mock(return_value=httpx.Response(200, text=FANDOM_JSON))

    await poll_source(fandom_wiki.FandomWikiSource(), conn, settings)

    assert "PTDP-TNRN-MDPP" in FANDOM_JSON, "fixture must still contain the trap"
    assert repo.get_code(conn, "PTDPTNRNMDPP") is None


@respx.mock
async def test_fandom_uses_the_api_not_the_rendered_page(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """Fandom serves 403 to anything that does not look like a browser, so the
    wiki page itself cannot be scraped. The API is public and returns wikitext."""
    route = fandom_route().mock(return_value=httpx.Response(200, text=FANDOM_JSON))

    await poll_source(fandom_wiki.FandomWikiSource(), conn, settings)

    params = route.calls.last.request.url.params
    assert params["page"] == "Combinations"
    assert params["prop"] == "wikitext"
    assert params["formatversion"] == "2"


@respx.mock
async def test_fandom_api_error_is_reported(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    fandom_route().mock(
        return_value=httpx.Response(
            200, json={"error": {"code": "missingtitle", "info": "The page does not exist."}}
        )
    )

    result = await poll_source(fandom_wiki.FandomWikiSource(), conn, settings)

    assert result.error is not None
    assert "does not exist" in result.error


@respx.mock
async def test_fandom_non_json_is_reported(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    fandom_route().mock(return_value=httpx.Response(200, text="<html>bot check</html>"))

    result = await poll_source(fandom_wiki.FandomWikiSource(), conn, settings)

    assert result.error is not None
    assert "not JSON" in result.error


# --------------------------------------------------------------------------
# shared fetch behaviour
# --------------------------------------------------------------------------


@respx.mock
async def test_the_etag_is_sent_back_on_the_next_poll(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    route = incendar_route().mock(
        side_effect=[
            httpx.Response(200, text=INCENDAR_HTML, headers={"ETag": '"abc123"'}),
            httpx.Response(304),
        ]
    )
    source = incendar.IncendarSource()

    first = await poll_source(source, conn, settings)
    second = await poll_source(source, conn, settings)

    assert route.calls[1].request.headers["If-None-Match"] == '"abc123"'
    assert first.added == 6
    assert second.found == 0 and second.error is None


@respx.mock
async def test_an_unchanged_page_still_reports_its_codes(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """Withholding codes the source can see would never heal a database restored
    from an older backup, so the digest only picks a log level."""
    incendar_route().mock(return_value=httpx.Response(200, text=INCENDAR_HTML))
    source = incendar.IncendarSource()

    await poll_source(source, conn, settings)
    stored = repo.get_code(conn, "MINIBEDSSIPE")
    assert stored is not None
    conn.execute("DELETE FROM codes WHERE id = ?", (stored.id,))

    again = await poll_source(source, conn, settings)

    assert again.found == 6
    assert repo.get_code(conn, "MINIBEDSSIPE") is not None


@respx.mock
async def test_overlapping_codes_are_stored_once(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """Both sites list many of the same codes, and so does Discord."""
    repo.add_code(conn, "CARAMONPARTY", source="discord")
    fandom_route().mock(return_value=httpx.Response(200, text=FANDOM_JSON))

    result = await poll_source(fandom_wiki.FandomWikiSource(), conn, settings)

    assert result.found == 5
    assert result.added == 4
    stored = repo.get_code(conn, "CARAMONPARTY")
    assert stored is not None and stored.source == "discord", "first sighting wins"


@respx.mock
async def test_a_bot_check_is_named(conn: sqlite3.Connection, settings: Settings) -> None:
    incendar_route().mock(return_value=httpx.Response(403, text="denied"))

    result = await poll_source(incendar.IncendarSource(), conn, settings)

    assert result.error is not None
    assert "bot check" in result.error


@respx.mock
async def test_a_network_failure_does_not_take_the_service_down(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    incendar_route().mock(side_effect=httpx.ConnectError("no route to host"))

    result = await poll_source(incendar.IncendarSource(), conn, settings)

    assert result.error is not None
    assert result.added == 0


@respx.mock
async def test_reset_forgets_the_page_state(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    incendar_route().mock(
        return_value=httpx.Response(200, text=INCENDAR_HTML, headers={"ETag": '"abc"'})
    )
    source = incendar.IncendarSource()
    await poll_source(source, conn, settings)
    assert source.position(conn) is not None

    source.reset(conn)

    assert source.position(conn) is None
    assert repo.kv_get(conn, "web:incendar:etag") is None
