"""Chest and blacksmith operations.

These are manual, explicit actions -- never scheduled. Buying chests and
spending blacksmith contracts consumes in-game resources, so nothing here is
ever driven by a timer (PLAN.md §7).

Requests larger than the per-call caps are split into successive paced calls
rather than rejected, matching what the extension's UI did.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from icr.game.api import (
    MAX_BLACKSMITH,
    MAX_BUY_CHESTS,
    MAX_OPEN_CHESTS,
    IdleChampionsApi,
)
from icr.game.models import (
    CONTRACT_LABELS,
    BlacksmithResult,
    ChestOpenResult,
    ChestType,
    ContractType,
    UserDetails,
)
from icr.game.session import GameSession
from icr.models import Account

log = logging.getLogger(__name__)

#: `add_inventory_buff_id` values for bounty drops, from the extension's
#: chest-management aggregation.
BOUNTY_IDS = {17: "common", 18: "uncommon", 19: "rare", 20: "epic"}


@dataclass
class ChestOpenSummary:
    chests_opened: int = 0
    gold: int = 0
    bounties: dict[str, int] = field(default_factory=dict)
    contracts: dict[int, int] = field(default_factory=dict)
    gear_new: int = 0
    gear_gilded: int = 0
    shinies: int = 0
    chests_remaining: int | None = None

    def add_loot(self, loot: list[dict[str, Any]]) -> None:
        for item in loot:
            if amount := item.get("add_gold_amount"):
                # Gold arrives as a stringified float on some chest types.
                with suppress(TypeError, ValueError):
                    self.gold += int(float(amount))

            buff_id = item.get("add_inventory_buff_id")
            if buff_id in BOUNTY_IDS:
                name = BOUNTY_IDS[int(buff_id)]
                self.bounties[name] = self.bounties.get(name, 0) + 1
            elif buff_id in {c.value for c in ContractType}:
                self.contracts[int(buff_id)] = self.contracts.get(int(buff_id), 0) + 1

            if item.get("new"):
                self.gear_new += 1
            if item.get("gilded"):
                self.gear_gilded += 1
                self.shinies += 1

    def describe(self) -> str:
        parts = [f"{self.chests_opened} chest(s) opened"]
        if self.gold:
            parts.append(f"{self.gold:,} gold")
        for rarity, count in self.bounties.items():
            parts.append(f"{count} {rarity} bounties")
        for contract_id, count in sorted(self.contracts.items()):
            label = CONTRACT_LABELS.get(ContractType(contract_id), str(contract_id))
            parts.append(f"{count} {label} contracts")
        if self.gear_new:
            parts.append(f"{self.gear_new} new gear")
        if self.gear_gilded:
            parts.append(f"{self.gear_gilded} gilded")
        if self.chests_remaining is not None:
            parts.append(f"{self.chests_remaining:,} remaining")
        return ", ".join(parts)


@dataclass
class BlacksmithSummary:
    contracts_used: int = 0
    upgrades: int = 0
    buffs_remaining: int | None = None

    def describe(self) -> str:
        parts = [f"{self.contracts_used} contract(s) used", f"{self.upgrades} upgrade(s)"]
        if self.buffs_remaining is not None:
            parts.append(f"{self.buffs_remaining:,} remaining")
        return ", ".join(parts)


def _chunks(total: int, cap: int) -> list[int]:
    """Split a request into per-call batches, largest first."""
    if total < 1:
        raise ValueError("count must be at least 1")
    full, remainder = divmod(total, cap)
    batches = [cap] * full
    if remainder:
        batches.append(remainder)
    return batches


async def fetch_inventory(
    conn: sqlite3.Connection, api: IdleChampionsApi, account: Account
) -> UserDetails:
    """Current chest and contract counts, refreshing the instance id as a
    side effect."""
    session = GameSession(conn, api)
    server = await session.refresh_instance_id(account)
    return await api.get_user_details(
        server=server, user_id=account.user_id, user_hash=account.user_hash
    )


async def open_chests(
    conn: sqlite3.Connection,
    api: IdleChampionsApi,
    account: Account,
    *,
    chest_type: ChestType | int,
    count: int,
) -> ChestOpenSummary:
    session = GameSession(conn, api)
    summary = ChestOpenSummary()

    for batch in _chunks(count, MAX_OPEN_CHESTS):

        async def op(server: str, instance_id: str, n: int = batch) -> ChestOpenResult:
            return await api.open_chests(
                server=server,
                user_id=account.user_id,
                user_hash=account.user_hash,
                instance_id=instance_id,
                chest_type=chest_type,
                count=n,
            )

        result = await session.call(account, op)
        summary.chests_opened += batch
        summary.add_loot(result.loot)
        summary.chests_remaining = result.chests_remaining
        log.info("Account %s: opened %d chest(s).", account.name, batch)

    return summary


async def buy_chests(
    conn: sqlite3.Connection,
    api: IdleChampionsApi,
    account: Account,
    *,
    chest_type: ChestType | int,
    count: int,
) -> int:
    """Spends in-game currency. Returns the number bought."""
    session = GameSession(conn, api)
    bought = 0

    for batch in _chunks(count, MAX_BUY_CHESTS):

        async def op(server: str, instance_id: str, n: int = batch) -> None:
            await api.purchase_chests(
                server=server,
                user_id=account.user_id,
                user_hash=account.user_hash,
                chest_type=chest_type,
                count=n,
            )

        await session.call(account, op)
        bought += batch
        log.info("Account %s: bought %d chest(s).", account.name, batch)

    return bought


async def use_blacksmith(
    conn: sqlite3.Connection,
    api: IdleChampionsApi,
    account: Account,
    *,
    contract: ContractType | int,
    hero_id: str,
    count: int,
) -> BlacksmithSummary:
    """Spends blacksmith contracts on one hero's gear."""
    session = GameSession(conn, api)
    summary = BlacksmithSummary()

    for batch in _chunks(count, MAX_BLACKSMITH):

        async def op(server: str, instance_id: str, n: int = batch) -> BlacksmithResult:
            return await api.use_blacksmith(
                server=server,
                user_id=account.user_id,
                user_hash=account.user_hash,
                instance_id=instance_id,
                contract=contract,
                hero_id=hero_id,
                count=n,
            )

        result = await session.call(account, op)
        summary.contracts_used += batch
        summary.upgrades += len(result.actions)
        summary.buffs_remaining = result.buffs_remaining
        log.info("Account %s: used %d contract(s) on hero %s.", account.name, batch, hero_id)

    return summary
