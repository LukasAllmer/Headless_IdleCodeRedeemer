from __future__ import annotations

import sqlite3

import httpx
import respx

from icr.db import repo
from icr.game.api import IdleChampionsApi
from icr.game.models import FailureReason
from icr.models import RedemptionStatus
from icr.redeemer import KV_PLAY_SERVER, Redeemer
from tests.conftest import PLAY_SERVER, failure, ok

SERVER_2 = "http://ps9.idlechampions.com/~idledragons/post.php"


def seed(conn: sqlite3.Connection, *names: str, code: str = "ABCDEFGHIJKL") -> None:
    for i, name in enumerate(names):
        repo.add_account(
            conn, name=name, user_id=f"u{i}", user_hash=f"h{i}", instance_id="inst"
        )
    repo.add_code(conn, code, source="manual")
    repo.kv_set(conn, KV_PLAY_SERVER, PLAY_SERVER)


def status_of(conn: sqlite3.Connection, account: str, code: str) -> RedemptionStatus:
    entries = repo.history(conn, account_name=account)
    match = next(e for e in entries if e.code == code)
    return match.status


@respx.mock
async def test_redeems_one_code_across_all_accounts(
    conn: sqlite3.Connection, api: IdleChampionsApi
) -> None:
    seed(conn, "main", "alt")
    respx.get(PLAY_SERVER).mock(
        return_value=ok(
            loot_details=[{"loot_action": "generic_chest", "chest_type_id": 2, "count": 3}]
        )
    )

    summary = await Redeemer(conn, api).run()

    assert summary.total_redeemed == 2
    assert summary.total_loot().chests == {2: 6}
    assert status_of(conn, "main", "ABCDEFGHIJKL") is RedemptionStatus.SUCCESS
    assert status_of(conn, "alt", "ABCDEFGHIJKL") is RedemptionStatus.SUCCESS
    # Nothing left outstanding.
    assert repo.outstanding_work(conn, max_attempts=3) == []


@respx.mock
async def test_switch_server_is_followed_and_cached(
    conn: sqlite3.Connection, api: IdleChampionsApi
) -> None:
    seed(conn, "main")
    respx.get(PLAY_SERVER).mock(
        return_value=ok(switch_play_server="http://ps9.idlechampions.com/~idledragons/")
    )
    second = respx.get(SERVER_2).mock(return_value=ok(loot_details=[]))

    summary = await Redeemer(conn, api).run()

    assert summary.total_redeemed == 1
    assert second.called
    # The new server is persisted so the next run starts there.
    assert repo.kv_get(conn, KV_PLAY_SERVER) == SERVER_2


@respx.mock
async def test_outdated_instance_id_is_refreshed_then_retried(
    conn: sqlite3.Connection, api: IdleChampionsApi
) -> None:
    seed(conn, "main")
    responses = [
        failure(FailureReason.OUTDATED_INSTANCE_ID),
        ok(details={"instance_id": "fresh-instance"}),
        ok(loot_details=[]),
    ]
    route = respx.get(PLAY_SERVER).mock(side_effect=responses)

    summary = await Redeemer(conn, api).run()

    assert summary.total_redeemed == 1
    assert repo.require_account(conn, "main").instance_id == "fresh-instance"
    # The retry carried the refreshed instance id.
    assert route.calls[-1].request.url.params["instance_id"] == "fresh-instance"


@respx.mock
async def test_switch_server_then_stale_instance_both_recover(
    conn: sqlite3.Connection, api: IdleChampionsApi
) -> None:
    """The sequence the extension could not handle: two recoveries in a row."""
    seed(conn, "main")
    respx.get(PLAY_SERVER).mock(
        return_value=ok(switch_play_server="http://ps9.idlechampions.com/~idledragons/")
    )
    respx.get(SERVER_2).mock(
        side_effect=[
            failure(FailureReason.OUTDATED_INSTANCE_ID),
            ok(details={"instance_id": "fresh"}),
            ok(loot_details=[]),
        ]
    )

    summary = await Redeemer(conn, api).run()

    assert summary.total_redeemed == 1
    assert repo.require_account(conn, "main").instance_id == "fresh"


@respx.mock
async def test_bad_credentials_skip_only_that_account(
    conn: sqlite3.Connection, api: IdleChampionsApi
) -> None:
    """The regression that mattered most: one bad account used to abort everything."""
    seed(conn, "broken", "healthy")
    repo.add_code(conn, "SECONDCODE12", source="manual")

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.params["user_id"] == "u0":  # the "broken" account
            return failure(FailureReason.INVALID_PARAMETERS)
        return ok(loot_details=[])

    respx.get(PLAY_SERVER).mock(side_effect=route)

    summary = await Redeemer(conn, api).run()

    broken = next(a for a in summary.accounts if a.account_name == "broken")
    healthy = next(a for a in summary.accounts if a.account_name == "healthy")

    assert broken.credentials_failed
    assert broken.skipped == 2  # both codes skipped, not just the failing one
    assert healthy.redeemed == 2  # unaffected
    # The account is flagged so later runs do not hammer it: its codes were
    # never recorded as attempted, but credentials_ok excludes them from work.
    assert repo.require_account(conn, "broken").credentials_ok is False
    assert repo.outstanding_work(conn, max_attempts=3) == []


@respx.mock
async def test_unknown_failure_does_not_abort_remaining_codes(
    conn: sqlite3.Connection, api: IdleChampionsApi
) -> None:
    """The other extension bug: an unrecognised reason aborted the whole batch."""
    seed(conn, "main", code="FIRSTCODE123")
    repo.add_code(conn, "SECONDCODE12", source="manual")

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.params["code"] == "FIRSTCODE123":
            return failure("brand_new_unhandled_reason")
        return ok(loot_details=[])

    respx.get(PLAY_SERVER).mock(side_effect=route)

    summary = await Redeemer(conn, api).run()

    assert summary.total_redeemed == 1
    assert summary.accounts[0].failed == 1
    assert status_of(conn, "main", "FIRSTCODE123") is RedemptionStatus.FAILED
    assert status_of(conn, "main", "SECONDCODE12") is RedemptionStatus.SUCCESS


@respx.mock
async def test_failed_codes_retry_until_ceiling(
    conn: sqlite3.Connection, api: IdleChampionsApi
) -> None:
    seed(conn, "main")
    respx.get(PLAY_SERVER).mock(return_value=httpx.Response(503))

    for _ in range(3):
        await Redeemer(conn, api, max_attempts=3).run()

    assert repo.outstanding_work(conn, max_attempts=3) == []
    assert status_of(conn, "main", "ABCDEFGHIJKL") is RedemptionStatus.FAILED


@respx.mock
async def test_terminal_statuses_are_never_retried(
    conn: sqlite3.Connection, api: IdleChampionsApi
) -> None:
    seed(conn, "main")
    route = respx.get(PLAY_SERVER).mock(
        return_value=failure(FailureReason.ALREADY_REDEEMED)
    )

    await Redeemer(conn, api).run()
    calls_after_first = len(route.calls)
    await Redeemer(conn, api).run()

    assert len(route.calls) == calls_after_first
    assert status_of(conn, "main", "ABCDEFGHIJKL") is RedemptionStatus.ALREADY_REDEEMED


@respx.mock
async def test_dry_run_makes_no_requests(
    conn: sqlite3.Connection, api: IdleChampionsApi
) -> None:
    seed(conn, "main")
    route = respx.get(PLAY_SERVER).mock(return_value=ok())

    summary = await Redeemer(conn, api, dry_run=True).run()

    assert not route.called
    assert summary.dry_run
    assert summary.accounts[0].skipped == 1
    assert repo.history(conn) == []


@respx.mock
async def test_disabled_account_is_skipped(
    conn: sqlite3.Connection, api: IdleChampionsApi
) -> None:
    seed(conn, "main", "alt")
    repo.set_account_enabled(conn, "alt", False)
    respx.get(PLAY_SERVER).mock(return_value=ok(loot_details=[]))

    summary = await Redeemer(conn, api).run()

    assert [a.account_name for a in summary.accounts] == ["main"]


@respx.mock
async def test_filters_narrow_the_run(
    conn: sqlite3.Connection, api: IdleChampionsApi
) -> None:
    seed(conn, "main", "alt")
    repo.add_code(conn, "SECONDCODE12", source="manual")
    respx.get(PLAY_SERVER).mock(return_value=ok(loot_details=[]))

    summary = await Redeemer(conn, api).run(account_name="main", code="SECONDCODE12")

    assert summary.total_attempted == 1
    assert summary.accounts[0].account_name == "main"
