"""Redemption orchestration.

Ported from the extension's `service_worker.ts`, with its three known defects
fixed (PLAN.md §1):

* A code that fails for an unrecognised reason no longer aborts the whole batch.
* Bad credentials skip the rest of *that account* rather than every account.
* Nothing is trimmed to fit a browser storage quota.

Everything runs sequentially through one paced API client. Volume is a handful
of codes a day across a handful of accounts, so concurrency would buy nothing
and only risk rate limits.
"""

from __future__ import annotations

import itertools
import json
import logging
import sqlite3
from dataclasses import dataclass, field

from icr.db import repo
from icr.game.api import IdleChampionsApi
from icr.game.errors import GameApiError, InvalidCredentialsError
from icr.game.models import CodeResult, CodeStatus, LootSummary
from icr.game.session import KV_PLAY_SERVER, GameSession
from icr.models import Account, RedemptionStatus, WorkItem

log = logging.getLogger(__name__)

__all__ = ["KV_PLAY_SERVER", "AccountSummary", "Redeemer", "RunSummary"]

STATUS_MAP = {
    CodeStatus.SUCCESS: RedemptionStatus.SUCCESS,
    CodeStatus.ALREADY_REDEEMED: RedemptionStatus.ALREADY_REDEEMED,
    CodeStatus.EXPIRED: RedemptionStatus.EXPIRED,
    CodeStatus.NOT_VALID_COMBO: RedemptionStatus.INVALID,
    CodeStatus.CANNOT_REDEEM: RedemptionStatus.CANNOT_REDEEM,
}


@dataclass
class AccountSummary:
    account_name: str
    redeemed: int = 0
    already_redeemed: int = 0
    expired: int = 0
    invalid: int = 0
    cannot_redeem: int = 0
    failed: int = 0
    skipped: int = 0
    credentials_failed: bool = False
    loot: LootSummary = field(default_factory=LootSummary)

    @property
    def attempted(self) -> int:
        return (
            self.redeemed
            + self.already_redeemed
            + self.expired
            + self.invalid
            + self.cannot_redeem
            + self.failed
        )

    def describe(self) -> str:
        if self.credentials_failed:
            return f"{self.account_name}: credentials rejected, {self.skipped} code(s) skipped"
        if self.attempted == 0:
            if self.skipped:
                # Dry run: nothing was sent, but there was work to send.
                return f"{self.account_name}: {self.skipped} code(s) would be attempted"
            return f"{self.account_name}: nothing to do"
        bits = [f"{self.redeemed} redeemed"]
        for count, label in (
            (self.already_redeemed, "already redeemed"),
            (self.expired, "expired"),
            (self.invalid, "invalid"),
            (self.cannot_redeem, "not redeemable"),
            (self.failed, "failed"),
        ):
            if count:
                bits.append(f"{count} {label}")
        line = f"{self.account_name}: " + ", ".join(bits)
        if not self.loot.is_empty:
            line += f" -- got {self.loot.describe()}"
        return line


@dataclass
class RunSummary:
    accounts: list[AccountSummary] = field(default_factory=list)
    dry_run: bool = False

    @property
    def total_redeemed(self) -> int:
        return sum(a.redeemed for a in self.accounts)

    @property
    def total_attempted(self) -> int:
        return sum(a.attempted for a in self.accounts)

    @property
    def had_work(self) -> bool:
        return any(a.attempted or a.skipped for a in self.accounts)

    def total_loot(self) -> LootSummary:
        combined = LootSummary()
        for account in self.accounts:
            combined.merge(account.loot)
        return combined

    def describe(self) -> str:
        if not self.had_work:
            return "Nothing to redeem."
        # Square brackets would be swallowed as markup by the CLI's rich console.
        prefix = "DRY RUN - " if self.dry_run else ""
        return prefix + "; ".join(a.describe() for a in self.accounts)


class Redeemer:
    def __init__(
        self,
        conn: sqlite3.Connection,
        api: IdleChampionsApi,
        *,
        max_attempts: int = 3,
        dry_run: bool = False,
    ) -> None:
        self._conn = conn
        self._api = api
        self._session = GameSession(conn, api)
        self._max_attempts = max_attempts
        self._dry_run = dry_run

    # ------------------------------------------------------------------
    # the run
    # ------------------------------------------------------------------

    async def run(
        self, *, account_name: str | None = None, code: str | None = None
    ) -> RunSummary:
        work = repo.outstanding_work(
            self._conn,
            max_attempts=self._max_attempts,
            account_name=account_name,
            code=code,
        )
        summary = RunSummary(dry_run=self._dry_run)
        if not work:
            log.info("No outstanding codes to redeem.")
            return summary

        log.info("%d outstanding (account, code) pair(s) to attempt.", len(work))

        for account_id, items in itertools.groupby(work, key=lambda w: w.account_id):
            batch = list(items)
            account = repo.get_account(self._conn, account_id)
            if account is None:  # deleted between query and run
                continue
            summary.accounts.append(await self._run_account(account, batch))

        log.info("Run complete. %s", summary.describe())
        return summary

    async def _run_account(self, account: Account, work: list[WorkItem]) -> AccountSummary:
        summary = AccountSummary(account_name=account.name)
        log.info("Account %s: %d code(s) to attempt.", account.name, len(work))

        for index, item in enumerate(work):
            if self._dry_run:
                log.info("[dry run] would redeem %s for %s", item.code, account.name)
                summary.skipped += 1
                continue

            try:
                result = await self._attempt(account, item)
            except InvalidCredentialsError:
                # The credentials themselves are bad. Every remaining code for
                # this account would fail identically, so stop here -- but other
                # accounts are unaffected and continue normally.
                remaining = len(work) - index
                log.error(
                    "Account %s: credentials rejected by the game server. "
                    "Skipping its remaining %d code(s). Fix with `icr account add "
                    "--name %s --support-url ...`.",
                    account.name,
                    remaining,
                    account.name,
                )
                repo.set_credentials_ok(self._conn, account.id, False)
                summary.credentials_failed = True
                summary.skipped += remaining
                break
            except GameApiError as exc:
                log.warning(
                    "Account %s: code %s failed (attempt %d/%d): %s",
                    account.name,
                    item.code,
                    item.attempts + 1,
                    self._max_attempts,
                    exc,
                )
                repo.record_redemption(
                    self._conn,
                    account_id=account.id,
                    code_id=item.code_id,
                    status=RedemptionStatus.FAILED,
                    error=str(exc),
                )
                summary.failed += 1
                continue

            self._record_success(account, item, result, summary)

        return summary

    def _record_success(
        self,
        account: Account,
        item: WorkItem,
        result: CodeResult,
        summary: AccountSummary,
    ) -> None:
        status = STATUS_MAP[result.status]
        loot_json = None

        if result.status is CodeStatus.SUCCESS:
            loot = LootSummary()
            loot.add(result.loot)
            summary.loot.merge(loot)
            summary.redeemed += 1
            loot_json = json.dumps(
                [
                    {
                        "loot_action": entry.loot_action,
                        "count": entry.count,
                        "hero_id": entry.hero_id,
                        "chest_type_id": entry.chest_type_id,
                        "unlock_hero_skin": entry.unlock_hero_skin,
                    }
                    for entry in result.loot
                ]
            )
            log.info("Account %s: redeemed %s -- %s", account.name, item.code, loot.describe())
        else:
            counter = {
                CodeStatus.ALREADY_REDEEMED: "already_redeemed",
                CodeStatus.EXPIRED: "expired",
                CodeStatus.NOT_VALID_COMBO: "invalid",
                CodeStatus.CANNOT_REDEEM: "cannot_redeem",
            }[result.status]
            setattr(summary, counter, getattr(summary, counter) + 1)
            log.info("Account %s: %s -> %s", account.name, item.code, status.value)

        repo.record_redemption(
            self._conn,
            account_id=account.id,
            code_id=item.code_id,
            status=status,
            loot_json=loot_json,
        )

    # ------------------------------------------------------------------
    # single code, with recovery
    # ------------------------------------------------------------------

    async def _attempt(self, account: Account, item: WorkItem) -> CodeResult:
        """Submit one code. `GameSession` absorbs server switches and stale
        instance ids, including two in a row -- a sequence the extension's
        retry-each-condition-once approach could not handle."""
        return await self._session.call(
            account,
            lambda server, instance_id: self._api.submit_code(
                server=server,
                user_id=account.user_id,
                user_hash=account.user_hash,
                instance_id=instance_id,
                code=item.code,
            ),
        )
