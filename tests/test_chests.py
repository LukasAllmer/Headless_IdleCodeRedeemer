from __future__ import annotations

import sqlite3

import pytest
import respx

from icr.chests import ChestOpenSummary, _chunks, buy_chests, open_chests, use_blacksmith
from icr.db import repo
from icr.game.api import IdleChampionsApi
from icr.game.models import ChestType, ContractType
from icr.redeemer import KV_PLAY_SERVER
from tests.conftest import PLAY_SERVER, ok


@pytest.fixture
def account(conn: sqlite3.Connection):
    repo.kv_set(conn, KV_PLAY_SERVER, PLAY_SERVER)
    return repo.add_account(
        conn, name="main", user_id="u1", user_hash="h1", instance_id="inst"
    )


# --------------------------------------------------------------------------
# chunking
# --------------------------------------------------------------------------


def test_chunks_exact_multiple() -> None:
    assert _chunks(2000, 1000) == [1000, 1000]


def test_chunks_with_remainder() -> None:
    assert _chunks(2500, 1000) == [1000, 1000, 500]


def test_chunks_below_cap() -> None:
    assert _chunks(7, 1000) == [7]


def test_chunks_rejects_zero() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        _chunks(0, 1000)


# --------------------------------------------------------------------------
# operations
# --------------------------------------------------------------------------


@respx.mock
async def test_open_chests_splits_into_capped_calls(
    conn: sqlite3.Connection, api: IdleChampionsApi, account
) -> None:
    route = respx.get(PLAY_SERVER).mock(
        return_value=ok(loot_details=[{"add_gold_amount": "500"}], chests_remaining=3)
    )

    summary = await open_chests(conn, api, account, chest_type=ChestType.GOLD, count=2500)

    assert len(route.calls) == 3
    assert [c.request.url.params["count"] for c in route.calls] == ["1000", "1000", "500"]
    assert summary.chests_opened == 2500
    assert summary.gold == 1500  # 500 per call, three calls
    assert summary.chests_remaining == 3


@respx.mock
async def test_buy_chests_splits_at_250(
    conn: sqlite3.Connection, api: IdleChampionsApi, account
) -> None:
    route = respx.get(PLAY_SERVER).mock(return_value=ok())

    bought = await buy_chests(conn, api, account, chest_type=ChestType.GOLD, count=600)

    assert bought == 600
    assert [c.request.url.params["count"] for c in route.calls] == ["250", "250", "100"]


@respx.mock
async def test_blacksmith_aggregates_upgrades(
    conn: sqlite3.Connection, api: IdleChampionsApi, account
) -> None:
    respx.get(PLAY_SERVER).mock(
        return_value=ok(actions=[{"action": "enchant"}, {"action": "enchant"}], buffs_remaining=8)
    )

    summary = await use_blacksmith(
        conn, api, account, contract=ContractType.LARGE, hero_id="42", count=1500
    )

    assert summary.contracts_used == 1500
    assert summary.upgrades == 4  # two calls, two actions each
    assert summary.buffs_remaining == 8


# --------------------------------------------------------------------------
# loot aggregation
# --------------------------------------------------------------------------


def test_summary_counts_bounties_and_contracts() -> None:
    summary = ChestOpenSummary()
    summary.add_loot(
        [
            {"add_inventory_buff_id": 17},
            {"add_inventory_buff_id": 17},
            {"add_inventory_buff_id": 20},
            {"add_inventory_buff_id": 34},
            {"new": True},
            {"gilded": True},
        ]
    )
    assert summary.bounties == {"common": 2, "epic": 1}
    assert summary.contracts == {ContractType.LARGE: 1}
    assert summary.gear_new == 1
    assert summary.gear_gilded == 1


def test_summary_tolerates_unparseable_gold() -> None:
    """Gold arrives as a stringified float, and occasionally as nonsense."""
    summary = ChestOpenSummary()
    summary.add_loot([{"add_gold_amount": "1.5e3"}, {"add_gold_amount": "not a number"}])
    assert summary.gold == 1500
