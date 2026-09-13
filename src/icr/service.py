"""The long-running service: scheduled source polling, queue draining, web UI.

Everything shares one asyncio loop and one SQLite connection. The scheduler and
the web UI are both producers of work against the same database, so a lock
serialises them -- SQLite tolerates concurrent access, but two overlapping
redeem runs would double-submit the same code.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from icr import sources
from icr.config import Settings, get_settings
from icr.db import DatabaseUnwritableError, ensure_writable, open_db, repo
from icr.game.api import IdleChampionsApi, build_client
from icr.logging_setup import register_secret, run_context, setup_logging
from icr.redeemer import Redeemer, RunSummary

log = logging.getLogger(__name__)


@dataclass
class ServiceState:
    """Shared handle passed to the web layer."""

    settings: Settings
    conn: sqlite3.Connection
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_poll: list[sources.PollResult] = field(default_factory=list)
    last_run: RunSummary | None = None
    last_run_at: str | None = None

    async def poll_sources(self) -> list[sources.PollResult]:
        """Ask every enabled source for new codes."""
        async with self.lock, run_context_async():
            results = await sources.poll_all(self.conn, self.settings)
            self.last_poll = results
            log.info("Poll cycle: %s", sources.describe_cycle(results))
            for result in results:
                if result.error:
                    log.warning("Source %s failed: %s", result.source, result.error)
            return results

    async def redeem(
        self, *, account_name: str | None = None, code: str | None = None
    ) -> RunSummary:
        """Drain the outstanding work queue."""
        async with self.lock, run_context_async():
            async with build_client(self.settings.http_timeout_seconds) as client:
                api = IdleChampionsApi(
                    client, request_delay=self.settings.request_delay_seconds
                )
                redeemer = Redeemer(
                    self.conn, api, max_attempts=self.settings.max_redeem_attempts
                )
                summary = await redeemer.run(account_name=account_name, code=code)

            if summary.had_work:
                self.last_run = summary
                self.last_run_at = _now()
            return summary


def _now() -> str:
    from icr.db import utcnow

    return utcnow()


class run_context_async:
    """`run_context` as an async context manager, so log lines from one tick
    share a correlation id."""

    def __init__(self) -> None:
        self._ctx = run_context()

    async def __aenter__(self) -> str:
        return self._ctx.__enter__()

    async def __aexit__(self, *exc_info: object) -> None:
        self._ctx.__exit__(*exc_info)  # type: ignore[arg-type]


async def _tick_poll(state: ServiceState) -> None:
    try:
        await state.poll_sources()
    except Exception:
        # A scheduled job that raises would otherwise be silently swallowed by
        # APScheduler's default error handling.
        log.exception("Scheduled source poll failed")


async def _tick_redeem(state: ServiceState) -> None:
    try:
        await state.redeem()
    except Exception:
        log.exception("Scheduled redeem run failed")


async def _redeem_if_work(state: ServiceState) -> None:
    """Redeem only when the queue actually has something in it.

    Redemption has no timer of its own. Checking first is a local SQLite query,
    so an idle installation does no work and says nothing, instead of logging a
    redeem run every minute that had nothing to do.

    Gating on "is there outstanding work" rather than "did this poll find
    something new" is deliberate: it also covers codes added by `icr code add`
    from another process, a newly added account that has every existing code to
    catch up on, and retryable failures from an earlier run. None of those
    involve a source finding anything.
    """
    work = repo.outstanding_work(
        state.conn, max_attempts=state.settings.max_redeem_attempts
    )
    if not work:
        log.debug("Nothing outstanding; not starting a redeem run.")
        return
    log.info("%d (account, code) pair(s) outstanding -- redeeming.", len(work))
    await _tick_redeem(state)


async def _tick_cycle(state: ServiceState) -> None:
    """Poll every source, then redeem whatever that left outstanding.

    One job rather than two so the order is fixed: codes found by a poll are
    redeemed in the same cycle instead of waiting for a separate timer.
    """
    await _tick_poll(state)
    await _redeem_if_work(state)


def build_scheduler(state: ServiceState) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    settings = state.settings

    scheduler.add_job(
        _tick_cycle,
        IntervalTrigger(seconds=settings.source_poll_interval_seconds),
        args=[state],
        id="cycle",
        max_instances=1,
        coalesce=True,
        # Run immediately rather than after the first full interval. A restart
        # that sits idle for five minutes before looking at anything reads as
        # broken. misfire_grace_time=None runs it however late the loop gets
        # around to it, instead of dropping it after one second.
        next_run_time=datetime.now(UTC),
        misfire_grace_time=None,
    )

    enabled = [name for name, s in sources.REGISTRY.items() if s.enabled(settings)]
    if enabled:
        log.info(
            "Polling %s every %ds; redeeming whenever that leaves work outstanding.",
            ", ".join(enabled),
            settings.source_poll_interval_seconds,
        )
    else:
        log.warning(
            "No code sources are enabled -- only codes added by hand will be "
            "redeemed, on the next cycle or when you trigger one."
        )
    return scheduler


def run_service() -> None:
    """Entry point for `icr serve`."""
    settings = get_settings()
    setup_logging(
        level=settings.log_level,
        log_file=settings.log_file,
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
    )
    register_secret(*settings.secrets())

    # A restart loop printing a full traceback every 30 seconds buries the one
    # line that says what to fix, so this failure is reported the same way the
    # CLI reports it.
    try:
        ensure_writable(settings.db_path)
    except DatabaseUnwritableError as exc:
        log.error("%s", exc)
        raise SystemExit(2) from None

    with open_db(settings.db_path) as conn:
        repo.load_secrets(conn)
        state = ServiceState(settings=settings, conn=conn)

        accounts = repo.list_accounts(conn, enabled_only=True)
        log.info(
            "Starting icr with %d enabled account(s), database %s",
            len(accounts),
            settings.db_path,
        )
        if not accounts:
            log.warning("No enabled accounts. Add one with `icr account add`.")

        if settings.web_enabled:
            _run_with_web(state)
        else:
            asyncio.run(_run_headless(state))


async def _run_headless(state: ServiceState) -> None:
    scheduler = build_scheduler(state)
    scheduler.start()
    log.info("Running without the web UI (ICR_WEB_ENABLED=false).")
    try:
        await asyncio.Event().wait()  # run until signalled
    finally:
        scheduler.shutdown(wait=False)


def _run_with_web(state: ServiceState) -> None:
    from icr.web.app import create_app

    app = create_app(state)
    config = uvicorn.Config(
        app,
        host=state.settings.web_host,
        port=state.settings.web_port,
        log_config=None,  # keep our own logging setup
        access_log=False,
    )
    if state.settings.web_auth_token:
        note = ""
    elif state.settings.web_insecure_bind:
        note = " (NO AUTH TOKEN -- relying on the port being published to loopback)"
    else:
        note = " (no auth token; loopback only)"
    log.info(
        "Web UI on http://%s:%d%s", state.settings.web_host, state.settings.web_port, note
    )
    uvicorn.Server(config).run()
