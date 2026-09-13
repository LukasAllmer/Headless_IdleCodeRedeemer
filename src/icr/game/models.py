"""Idle Champions API value types.

Ported from the extension's `src/lib/redeem_code_response.d.ts` and
`src/shared/idle_champions_api.ts`. Only the fields the service actually reads
are modelled -- the real `getuserdetails` payload is enormous and almost
entirely irrelevant here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any


class FailureReason(StrEnum):
    """`failure_reason` strings returned by the game server."""

    OUTDATED_INSTANCE_ID = "Outdated instance id"
    ALREADY_REDEEMED = "you_already_redeemed_combination"
    SOMEONE_ALREADY_REDEEMED = "someone_already_redeemed_combination"
    INVALID_PARAMETERS = "Invalid or incomplete parameters"
    NOT_VALID_COMBO = "not_valid_combination"
    EXPIRED = "offer_has_expired"
    NOT_ENOUGH_CURRENCY = "Not enough currency"
    CANNOT_REDEEM = "can_not_redeem_combination"


class ChestType(IntEnum):
    SILVER = 1
    GOLD = 2
    MODRON = 230
    ELECTRUM = 282


class ContractType(IntEnum):
    """Blacksmith contract ids, which double as `add_inventory_buff_id` values."""

    TINY = 31
    SMALL = 32
    MEDIUM = 33
    LARGE = 34


class LootAction(StrEnum):
    HERO_UNLOCK = "unlock_hero"
    CHEST = "generic_chest"
    CLAIM = "claim"


CHEST_LABELS = {
    ChestType.SILVER: "Silver Chests",
    ChestType.GOLD: "Gold Chests",
    ChestType.MODRON: "Modron Chests",
    ChestType.ELECTRUM: "Electrum Chests",
}

CONTRACT_LABELS = {
    ContractType.TINY: "Tiny (white)",
    ContractType.SMALL: "Small (green)",
    ContractType.MEDIUM: "Medium (blue)",
    ContractType.LARGE: "Large (purple)",
}


class CodeStatus(StrEnum):
    """Terminal outcome of a single `redeemcoupon` call.

    Retry signals (server switch, stale instance id) are raised as exceptions
    rather than represented here -- see `icr.game.errors`.
    """

    SUCCESS = "success"
    ALREADY_REDEEMED = "already_redeemed"
    NOT_VALID_COMBO = "not_valid_combo"
    EXPIRED = "expired"
    CANNOT_REDEEM = "cannot_redeem"


@dataclass(slots=True)
class LootItem:
    loot_action: str
    count: int | None = None
    hero_id: int | None = None
    chest_type_id: int | None = None
    unlock_hero_skin: int | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> LootItem:
        return cls(
            loot_action=str(data.get("loot_action", "")),
            count=_as_int(data.get("count")),
            hero_id=_as_int(data.get("hero_id")),
            chest_type_id=_as_int(data.get("chest_type_id")),
            unlock_hero_skin=_as_int(data.get("unlock_hero_skin")),
        )


@dataclass(slots=True)
class CodeResult:
    status: CodeStatus
    loot: list[LootItem] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.status is CodeStatus.SUCCESS


@dataclass(slots=True)
class LootSummary:
    """Aggregated rewards across a redeem run."""

    chests: dict[int, int] = field(default_factory=dict)
    hero_unlocks: int = 0
    skin_unlocks: int = 0

    def add(self, loot: list[LootItem]) -> None:
        for item in loot:
            if item.loot_action == LootAction.CHEST:
                if item.chest_type_id is not None and item.count:
                    self.chests[item.chest_type_id] = (
                        self.chests.get(item.chest_type_id, 0) + item.count
                    )
            elif item.loot_action == LootAction.HERO_UNLOCK:
                self.hero_unlocks += 1
            elif item.loot_action == LootAction.CLAIM and item.unlock_hero_skin:
                self.skin_unlocks += 1

    def merge(self, other: LootSummary) -> None:
        for chest_id, count in other.chests.items():
            self.chests[chest_id] = self.chests.get(chest_id, 0) + count
        self.hero_unlocks += other.hero_unlocks
        self.skin_unlocks += other.skin_unlocks

    @property
    def is_empty(self) -> bool:
        return not self.chests and not self.hero_unlocks and not self.skin_unlocks

    def describe(self) -> str:
        parts = [
            f"{count} x {chest_label(cid)}" for cid, count in sorted(self.chests.items())
        ]
        if self.hero_unlocks:
            parts.append(f"{self.hero_unlocks} hero unlock(s)")
        if self.skin_unlocks:
            parts.append(f"{self.skin_unlocks} skin unlock(s)")
        return ", ".join(parts) if parts else "nothing"


def chest_label(chest_type_id: int) -> str:
    """Chest types the game adds later show up as bare ids rather than crashing."""
    try:
        return CHEST_LABELS[ChestType(chest_type_id)]
    except ValueError:
        return f"chest type {chest_type_id}"


@dataclass(slots=True)
class UserDetails:
    """The handful of `getuserdetails` fields the service uses."""

    instance_id: str
    chests: dict[int, int] = field(default_factory=dict)
    contracts: dict[int, int] = field(default_factory=dict)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> UserDetails:
        details = payload.get("details") or {}

        chests: dict[int, int] = {}
        for key, value in (details.get("chests") or {}).items():
            chest_id, count = _as_int(key), _as_int(value)
            if chest_id is not None and count is not None:
                chests[chest_id] = count

        contracts: dict[int, int] = {}
        for buff in details.get("buffs") or []:
            buff_id = _as_int(buff.get("buff_id"))
            amount = _as_int(buff.get("inventory_amount"))
            if buff_id is not None and amount is not None:
                contracts[buff_id] = amount

        return cls(
            instance_id=str(details.get("instance_id", "")),
            chests=chests,
            contracts=contracts,
        )


@dataclass(slots=True)
class ChestOpenResult:
    loot: list[dict[str, Any]]
    chests_remaining: int | None = None


@dataclass(slots=True)
class BlacksmithResult:
    actions: list[dict[str, Any]]
    buffs_remaining: int | None = None


def _as_int(value: Any) -> int | None:
    """The API is loose about numeric types -- ids and counts arrive as both
    JSON numbers and strings depending on the endpoint."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
