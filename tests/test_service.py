from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from icr import service, sources
from icr.config import Settings
from icr.db import repo
from icr.models import RedemptionStatus
from icr.service import ServiceState, build_scheduler


@pytest.fixture
def state(conn: sqlite3.Connection, tmp_path) -> ServiceState:
    """No sources enabled, so these tests are about the schedule, not the web."""
    return ServiceState(
        settings=Settings(
            db_path=tmp_path / "t.sqlite3",
            incendar_enabled=False,
            fandom_enabled=False,
        ),
        conn=conn,
    )


def job_ids(state: ServiceState) -> list[str]:
    scheduler = build_scheduler(state)
    return [job.id for job in scheduler.get_jobs()]


def test_redemption_has_no_timer_of_its_own(state: ServiceState) -> None:
    """One job, not two. Redeeming happens after a poll that leaves work, or
    when something triggers it -- never on a clock."""
    assert job_ids(state) == ["cycle"]


def test_the_cycle_runs_immediately_rather_than_after_one_interval(
    state: ServiceState,
) -> None:
    """Otherwise a restart sits idle for a whole poll interval before it looks
    at anything, which reads as broken."""
    scheduler = build_scheduler(state)
    job = next(j for j in scheduler.get_jobs() if j.id == "cycle")
    assert job.next_run_time is not None
    assert job.next_run_time <= datetime.now(UTC) + timedelta(seconds=1)


def test_the_cycle_is_scheduled_even_with_no_sources_enabled(
    state: ServiceState,
) -> None:
    """Codes added by hand still need draining."""
    assert not any(s.enabled(state.settings) for s in sources.REGISTRY.values())
    assert "cycle" in job_ids(state)


async def test_a_cycle_polls_before_it_redeems(
    state: ServiceState, monkeypatch
) -> None:
    """Codes found by a poll are redeemed in the same cycle rather than waiting
    for anything else, which is only true if the order is fixed."""
    order: list[str] = []

    async def fake_poll(_: ServiceState) -> None:
        order.append("poll")

    async def fake_redeem(_: ServiceState) -> None:
        order.append("redeem")

    monkeypatch.setattr(service, "_tick_poll", fake_poll)
    monkeypatch.setattr(service, "_redeem_if_work", fake_redeem)

    await service._tick_cycle(state)

    assert order == ["poll", "redeem"]


async def test_a_failing_source_does_not_stop_the_redeem(
    state: ServiceState, monkeypatch
) -> None:
    async def exploding() -> list:
        raise RuntimeError("discord is down")

    redeemed: list[str] = []

    async def fake_redeem(_: ServiceState) -> None:
        redeemed.append("redeem")

    monkeypatch.setattr(state, "poll_sources", exploding)
    monkeypatch.setattr(service, "_redeem_if_work", fake_redeem)

    await service._tick_cycle(state)

    assert redeemed == ["redeem"]


# --------------------------------------------------------------------------
# what actually starts a redeem run
# --------------------------------------------------------------------------


async def test_an_idle_queue_starts_no_redeem_run(
    state: ServiceState, monkeypatch
) -> None:
    """The whole point of dropping the timer: an installation with nothing to do
    does nothing and says nothing."""
    ran: list[str] = []

    async def fake_redeem(_: ServiceState) -> None:
        ran.append("redeem")

    monkeypatch.setattr(service, "_tick_redeem", fake_redeem)

    await service._redeem_if_work(state)

    assert ran == []


async def test_outstanding_work_starts_a_run(
    state: ServiceState, conn: sqlite3.Connection, monkeypatch
) -> None:
    repo.add_account(conn, name="main", user_id="u1", user_hash="h1")
    repo.add_code(conn, "ABCDEFGHIJKL", source="manual")

    ran: list[str] = []

    async def fake_redeem(_: ServiceState) -> None:
        ran.append("redeem")

    monkeypatch.setattr(service, "_tick_redeem", fake_redeem)

    await service._redeem_if_work(state)

    assert ran == ["redeem"]


async def test_a_code_added_by_another_process_is_picked_up(
    state: ServiceState, conn: sqlite3.Connection, monkeypatch
) -> None:
    """`icr code add` runs in its own process and cannot signal the service, so
    the gate is 'is there work', not 'did a source just find something'."""
    repo.add_account(conn, name="main", user_id="u1", user_hash="h1")

    ran: list[str] = []

    async def fake_poll(_: ServiceState) -> None:
        pass  # every source finds nothing

    async def fake_redeem(_: ServiceState) -> None:
        ran.append("redeem")

    monkeypatch.setattr(service, "_tick_poll", fake_poll)
    monkeypatch.setattr(service, "_tick_redeem", fake_redeem)

    repo.add_code(conn, "ADDEDBYTHECLI", source="manual")  # the other process
    await service._tick_cycle(state)

    assert ran == ["redeem"]


async def test_a_retryable_failure_is_retried_on_the_next_cycle(
    state: ServiceState, conn: sqlite3.Connection, monkeypatch
) -> None:
    """Retries have no timer either -- they ride the next cycle, which is why
    dropping the redeem tick does not strand them."""
    account = repo.add_account(conn, name="main", user_id="u1", user_hash="h1")
    code, _ = repo.add_code(conn, "ABCDEFGHIJKL", source="manual")
    repo.record_redemption(
        conn,
        account_id=account.id,
        code_id=code.id,
        status=RedemptionStatus.FAILED,
        error="the game server was down",
    )

    ran: list[str] = []

    async def fake_redeem(_: ServiceState) -> None:
        ran.append("redeem")

    monkeypatch.setattr(service, "_tick_redeem", fake_redeem)

    await service._redeem_if_work(state)

    assert ran == ["redeem"]


async def test_a_code_that_ran_out_of_attempts_stops_waking_the_service(
    state: ServiceState, conn: sqlite3.Connection, monkeypatch
) -> None:
    account = repo.add_account(conn, name="main", user_id="u1", user_hash="h1")
    code, _ = repo.add_code(conn, "ABCDEFGHIJKL", source="manual")
    for _ in range(state.settings.max_redeem_attempts):
        repo.record_redemption(
            conn,
            account_id=account.id,
            code_id=code.id,
            status=RedemptionStatus.FAILED,
            error="still down",
        )

    ran: list[str] = []

    async def fake_redeem(_: ServiceState) -> None:
        ran.append("redeem")

    monkeypatch.setattr(service, "_tick_redeem", fake_redeem)

    await service._redeem_if_work(state)

    assert ran == []
