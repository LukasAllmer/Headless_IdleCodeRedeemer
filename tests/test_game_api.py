from __future__ import annotations

import httpx
import pytest
import respx

from icr.game.api import MASTER_URL, IdleChampionsApi
from icr.game.errors import (
    InsufficientCurrencyError,
    InvalidCredentialsError,
    OutdatedInstanceIdError,
    RequestFailedError,
    SwitchServerError,
)
from icr.game.models import ChestType, CodeStatus, ContractType, FailureReason
from tests.conftest import PLAY_SERVER, failure, ok

CREDS = {"user_id": "12345", "user_hash": "deadbeef"}


@respx.mock
async def test_get_play_server_appends_post_php(api: IdleChampionsApi) -> None:
    respx.get(MASTER_URL).mock(
        return_value=ok(play_server="http://ps7.idlechampions.com/~idledragons/")
    )
    assert await api.get_play_server() == PLAY_SERVER


@respx.mock
async def test_get_play_server_missing_field(api: IdleChampionsApi) -> None:
    respx.get(MASTER_URL).mock(return_value=ok())
    with pytest.raises(RequestFailedError, match="play_server"):
        await api.get_play_server()


@respx.mock
async def test_submit_code_success_parses_loot(api: IdleChampionsApi) -> None:
    respx.get(PLAY_SERVER).mock(
        return_value=ok(
            loot_details=[
                {"loot_action": "generic_chest", "chest_type_id": 2, "count": 5},
                {"loot_action": "unlock_hero", "hero_id": 88},
            ]
        )
    )
    result = await api.submit_code(
        server=PLAY_SERVER, instance_id="i1", code="ABCDEFGHIJKL", **CREDS
    )
    assert result.status is CodeStatus.SUCCESS
    assert result.loot[0].chest_type_id == 2
    assert result.loot[0].count == 5


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (FailureReason.ALREADY_REDEEMED, CodeStatus.ALREADY_REDEEMED),
        (FailureReason.SOMEONE_ALREADY_REDEEMED, CodeStatus.ALREADY_REDEEMED),
        (FailureReason.EXPIRED, CodeStatus.EXPIRED),
        (FailureReason.NOT_VALID_COMBO, CodeStatus.NOT_VALID_COMBO),
        (FailureReason.CANNOT_REDEEM, CodeStatus.CANNOT_REDEEM),
    ],
)
@respx.mock
async def test_submit_code_terminal_failures(
    api: IdleChampionsApi, reason: FailureReason, expected: CodeStatus
) -> None:
    respx.get(PLAY_SERVER).mock(return_value=failure(reason))
    result = await api.submit_code(
        server=PLAY_SERVER, instance_id="i1", code="ABCDEFGHIJKL", **CREDS
    )
    assert result.status is expected


@respx.mock
async def test_submit_code_switch_server_raises_with_endpoint(api: IdleChampionsApi) -> None:
    respx.get(PLAY_SERVER).mock(
        return_value=ok(switch_play_server="http://ps9.idlechampions.com/~idledragons/")
    )
    with pytest.raises(SwitchServerError) as excinfo:
        await api.submit_code(server=PLAY_SERVER, instance_id="i1", code="X", **CREDS)
    assert excinfo.value.new_server == "http://ps9.idlechampions.com/~idledragons/post.php"


@respx.mock
async def test_submit_code_outdated_instance_id(api: IdleChampionsApi) -> None:
    respx.get(PLAY_SERVER).mock(return_value=failure(FailureReason.OUTDATED_INSTANCE_ID))
    with pytest.raises(OutdatedInstanceIdError):
        await api.submit_code(server=PLAY_SERVER, instance_id="stale", code="X", **CREDS)


@respx.mock
async def test_submit_code_invalid_credentials(api: IdleChampionsApi) -> None:
    respx.get(PLAY_SERVER).mock(return_value=failure(FailureReason.INVALID_PARAMETERS))
    with pytest.raises(InvalidCredentialsError):
        await api.submit_code(server=PLAY_SERVER, instance_id="i1", code="X", **CREDS)


@respx.mock
async def test_submit_code_unknown_reason_is_retryable(api: IdleChampionsApi) -> None:
    respx.get(PLAY_SERVER).mock(return_value=failure("something_new_and_unhandled"))
    with pytest.raises(RequestFailedError, match="Unrecognised"):
        await api.submit_code(server=PLAY_SERVER, instance_id="i1", code="X", **CREDS)


@respx.mock
async def test_http_error_is_request_failed(api: IdleChampionsApi) -> None:
    respx.get(PLAY_SERVER).mock(return_value=httpx.Response(503))
    with pytest.raises(RequestFailedError, match="503"):
        await api.submit_code(server=PLAY_SERVER, instance_id="i1", code="X", **CREDS)


@respx.mock
async def test_non_json_body_is_request_failed(api: IdleChampionsApi) -> None:
    respx.get(PLAY_SERVER).mock(return_value=httpx.Response(200, text="<html>nope</html>"))
    with pytest.raises(RequestFailedError, match="not JSON"):
        await api.submit_code(server=PLAY_SERVER, instance_id="i1", code="X", **CREDS)


@respx.mock
async def test_transport_error_is_request_failed(api: IdleChampionsApi) -> None:
    respx.get(PLAY_SERVER).mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(RequestFailedError):
        await api.submit_code(server=PLAY_SERVER, instance_id="i1", code="X", **CREDS)


@respx.mock
async def test_get_user_details_extracts_fields(api: IdleChampionsApi) -> None:
    respx.get(PLAY_SERVER).mock(
        return_value=ok(
            details={
                "instance_id": "fresh-instance",
                "chests": {"1": 10, "2": "25"},
                "buffs": [
                    {"buff_id": "31", "inventory_amount": "4"},
                    {"buff_id": "34", "inventory_amount": 2},
                ],
            }
        )
    )
    details = await api.get_user_details(server=PLAY_SERVER, **CREDS)
    assert details.instance_id == "fresh-instance"
    assert details.chests == {ChestType.SILVER: 10, ChestType.GOLD: 25}
    assert details.contracts[ContractType.TINY] == 4
    assert details.contracts[ContractType.LARGE] == 2


@respx.mock
async def test_get_user_details_without_instance_id_fails(api: IdleChampionsApi) -> None:
    respx.get(PLAY_SERVER).mock(return_value=ok(details={}))
    with pytest.raises(RequestFailedError, match="instance_id"):
        await api.get_user_details(server=PLAY_SERVER, **CREDS)


@respx.mock
async def test_open_chests_omits_client_version(api: IdleChampionsApi) -> None:
    route = respx.get(PLAY_SERVER).mock(
        return_value=ok(loot_details=[{"rarity": 1}], chests_remaining=7)
    )
    result = await api.open_chests(
        server=PLAY_SERVER, instance_id="i1", chest_type=ChestType.GOLD, count=10, **CREDS
    )
    assert result.chests_remaining == 7
    params = route.calls.last.request.url.params
    assert "mobile_client_version" not in params
    assert params["checksum"] == "d99242bc7924646a5e069bc39eeb735b"


async def test_open_chests_enforces_cap(api: IdleChampionsApi) -> None:
    with pytest.raises(ValueError, match="1000"):
        await api.open_chests(
            server=PLAY_SERVER, instance_id="i1", chest_type=ChestType.GOLD, count=1001, **CREDS
        )


@respx.mock
async def test_purchase_chests_insufficient_currency(api: IdleChampionsApi) -> None:
    respx.get(PLAY_SERVER).mock(return_value=failure(FailureReason.NOT_ENOUGH_CURRENCY))
    with pytest.raises(InsufficientCurrencyError):
        await api.purchase_chests(
            server=PLAY_SERVER, chest_type=ChestType.GOLD, count=1, **CREDS
        )


async def test_purchase_chests_enforces_cap(api: IdleChampionsApi) -> None:
    with pytest.raises(ValueError, match="250"):
        await api.purchase_chests(
            server=PLAY_SERVER, chest_type=ChestType.GOLD, count=251, **CREDS
        )


@respx.mock
async def test_use_blacksmith_returns_actions(api: IdleChampionsApi) -> None:
    respx.get(PLAY_SERVER).mock(
        return_value=ok(
            actions=[{"action": "enchant", "hero_id": "42", "enchant_level": 3}],
            buffs_remaining=12,
        )
    )
    result = await api.use_blacksmith(
        server=PLAY_SERVER,
        instance_id="i1",
        contract=ContractType.LARGE,
        hero_id="42",
        count=5,
        **CREDS,
    )
    assert result.buffs_remaining == 12
    assert result.actions[0]["enchant_level"] == 3


async def test_use_blacksmith_enforces_cap(api: IdleChampionsApi) -> None:
    with pytest.raises(ValueError, match="1000"):
        await api.use_blacksmith(
            server=PLAY_SERVER,
            instance_id="i1",
            contract=ContractType.LARGE,
            hero_id="42",
            count=1001,
            **CREDS,
        )
