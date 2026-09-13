"""Idle Champions HTTP client.

A direct port of the extension's `src/shared/idle_champions_api.ts`. Query
parameters, constants and the hardcoded chest checksum are carried over
unchanged -- they are what the game server actually accepts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from icr.game.errors import (
    InsufficientCurrencyError,
    InvalidCredentialsError,
    OutdatedInstanceIdError,
    RequestFailedError,
    SwitchServerError,
)
from icr.game.models import (
    BlacksmithResult,
    ChestOpenResult,
    ChestType,
    CodeResult,
    CodeStatus,
    ContractType,
    FailureReason,
    LootItem,
    UserDetails,
)

log = logging.getLogger(__name__)

MASTER_URL = "https://master.idlechampions.com/~idledragons/post.php"

#: Sentinel version the extension used to sidestep client version checks. If the
#: game starts rejecting requests, this is the first thing to bump (PLAN.md §13).
CLIENT_VERSION = "999"
NETWORK_ID = "21"
LANGUAGE_ID = "1"

MAX_BUY_CHESTS = 250
MAX_OPEN_CHESTS = 1000
MAX_BLACKSMITH = 1000

_CODE_FAILURES = {
    FailureReason.ALREADY_REDEEMED: CodeStatus.ALREADY_REDEEMED,
    FailureReason.SOMEONE_ALREADY_REDEEMED: CodeStatus.ALREADY_REDEEMED,
    FailureReason.EXPIRED: CodeStatus.EXPIRED,
    FailureReason.NOT_VALID_COMBO: CodeStatus.NOT_VALID_COMBO,
    FailureReason.CANNOT_REDEEM: CodeStatus.CANNOT_REDEEM,
}


def _server_url(base: str) -> str:
    """`play_server` and `switch_play_server` are directory URLs; the endpoint is
    `post.php` beneath them."""
    return base if base.endswith("post.php") else base.rstrip("/") + "/post.php"


class IdleChampionsApi:
    """Paced async client.

    All game-server calls funnel through `_get`, which enforces a global minimum
    gap between requests. The pacing is deliberately process-wide rather than
    per-account so that adding accounts does not multiply the request rate.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        request_delay: float = 2.0,
    ) -> None:
        self._client = client
        self._request_delay = request_delay
        self._last_request_at = 0.0
        self._pace_lock = asyncio.Lock()

    async def _pace(self) -> None:
        async with self._pace_lock:
            elapsed = time.monotonic() - self._last_request_at
            wait = self._request_delay - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = time.monotonic()

    async def _get(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        await self._pace()
        log.debug("GET %s call=%s", url, params.get("call"))
        try:
            response = await self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise RequestFailedError(f"Request to {url} failed: {exc}") from exc

        if response.status_code != httpx.codes.OK:
            raise RequestFailedError(f"{params.get('call')} returned HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise RequestFailedError(
                f"{params.get('call')} returned a body that is not JSON"
            ) from exc

        if not isinstance(payload, dict):
            raise RequestFailedError(f"{params.get('call')} returned {type(payload).__name__}")
        return payload

    @staticmethod
    def _check_common(payload: dict[str, Any]) -> None:
        """Raise for the failure modes shared by every endpoint."""
        new_server = payload.get("switch_play_server")
        if new_server:
            raise SwitchServerError(_server_url(str(new_server)))

        reason = payload.get("failure_reason")
        if reason == FailureReason.OUTDATED_INSTANCE_ID:
            raise OutdatedInstanceIdError("Instance id is stale")
        if reason == FailureReason.INVALID_PARAMETERS:
            raise InvalidCredentialsError("Game server rejected user_id/hash")

    def _common_params(self, call: str, *, client_version: bool = True) -> dict[str, str]:
        """Parameters every call carries.

        `openGenericChest` and `useServerBuff` are the two endpoints the
        extension called *without* `mobile_client_version`; that asymmetry is
        preserved rather than tidied, since these are the parameter sets known
        to work against the live server.
        """
        params = {
            "call": call,
            "timestamp": "0",
            "request_id": "0",
            "network_id": NETWORK_ID,
            "language_id": LANGUAGE_ID,
            "localization_aware": "true",
        }
        if client_version:
            params["mobile_client_version"] = CLIENT_VERSION
        return params

    # ----------------------------------------------------------------------
    # endpoints
    # ----------------------------------------------------------------------

    async def get_play_server(self) -> str:
        """Resolve the current play server endpoint."""
        params = {
            "call": "getPlayServerForDefinitions",
            "mobile_client_version": CLIENT_VERSION,
            "network_id": NETWORK_ID,
            "timestamp": "0",
            "request_id": "0",
            "localization_aware": "true",
        }
        payload = await self._get(MASTER_URL, params)
        play_server = payload.get("play_server")
        if not play_server:
            raise RequestFailedError("Server definitions did not include a play_server")
        return _server_url(str(play_server))

    async def submit_code(
        self,
        *,
        server: str,
        user_id: str,
        user_hash: str,
        instance_id: str,
        code: str,
    ) -> CodeResult:
        """Redeem one code.

        Raises `SwitchServerError` / `OutdatedInstanceIdError` /
        `InvalidCredentialsError` for the recoverable cases; the caller decides
        how to react.
        """
        params = self._common_params("redeemcoupon") | {
            "user_id": user_id,
            "hash": user_hash,
            "code": code,
            "instance_id": instance_id,
        }
        payload = await self._get(server, params)
        self._check_common(payload)

        reason = payload.get("failure_reason")
        if reason in _CODE_FAILURES:
            return CodeResult(status=_CODE_FAILURES[FailureReason(reason)])

        if payload.get("success") and payload.get("okay"):
            loot = [LootItem.from_json(item) for item in payload.get("loot_details") or []]
            return CodeResult(status=CodeStatus.SUCCESS, loot=loot)

        raise RequestFailedError(f"Unrecognised redeemcoupon response (failure_reason={reason!r})")

    async def get_user_details(
        self, *, server: str, user_id: str, user_hash: str
    ) -> UserDetails:
        params = self._common_params("getuserdetails") | {
            "user_id": user_id,
            "hash": user_hash,
            "instance_key": "0",
            "include_free_play_objectives": "true",
        }
        payload = await self._get(server, params)
        self._check_common(payload)

        if not payload.get("success"):
            raise RequestFailedError(
                f"getuserdetails failed (failure_reason={payload.get('failure_reason')!r})"
            )

        details = UserDetails.from_json(payload)
        if not details.instance_id:
            raise RequestFailedError("getuserdetails response contained no instance_id")
        return details

    async def open_chests(
        self,
        *,
        server: str,
        user_id: str,
        user_hash: str,
        instance_id: str,
        chest_type: ChestType | int,
        count: int,
    ) -> ChestOpenResult:
        if count > MAX_OPEN_CHESTS:
            raise ValueError(f"Cannot open more than {MAX_OPEN_CHESTS} chests per call")

        params = self._common_params("openGenericChest", client_version=False) | {
            "user_id": user_id,
            "hash": user_hash,
            "chest_type_id": str(int(chest_type)),
            "count": str(count),
            "instance_id": instance_id,
            "gold_per_second": "0.00",
            "game_instance_id": "1",
            # Carried over verbatim from the extension: the server accepts this
            # fixed value rather than validating a real checksum.
            "checksum": "d99242bc7924646a5e069bc39eeb735b",
        }
        payload = await self._get(server, params)
        self._check_common(payload)

        if not payload.get("success"):
            raise RequestFailedError(
                f"openGenericChest failed (failure_reason={payload.get('failure_reason')!r})"
            )
        return ChestOpenResult(
            loot=list(payload.get("loot_details") or []),
            chests_remaining=payload.get("chests_remaining"),
        )

    async def purchase_chests(
        self,
        *,
        server: str,
        user_id: str,
        user_hash: str,
        chest_type: ChestType | int,
        count: int,
    ) -> None:
        if count > MAX_BUY_CHESTS:
            raise ValueError(f"Cannot buy more than {MAX_BUY_CHESTS} chests per call")

        params = self._common_params("buysoftcurrencychest") | {
            "user_id": user_id,
            "hash": user_hash,
            "chest_type_id": str(int(chest_type)),
            "count": str(count),
        }
        payload = await self._get(server, params)
        self._check_common(payload)

        if payload.get("failure_reason") == FailureReason.NOT_ENOUGH_CURRENCY:
            raise InsufficientCurrencyError("Not enough currency to buy chests")
        if not (payload.get("success") and payload.get("okay")):
            raise RequestFailedError(
                f"buysoftcurrencychest failed "
                f"(failure_reason={payload.get('failure_reason')!r})"
            )

    async def use_blacksmith(
        self,
        *,
        server: str,
        user_id: str,
        user_hash: str,
        instance_id: str,
        contract: ContractType | int,
        hero_id: str,
        count: int,
    ) -> BlacksmithResult:
        if count > MAX_BLACKSMITH:
            raise ValueError(f"Cannot use more than {MAX_BLACKSMITH} contracts per call")

        params = self._common_params("useServerBuff", client_version=False) | {
            "user_id": user_id,
            "hash": user_hash,
            "buff_id": str(int(contract)),
            "hero_id": hero_id,
            "num_uses": str(count),
            "instance_id": instance_id,
            "game_instance_id": "1",
        }
        payload = await self._get(server, params)
        self._check_common(payload)

        if not (payload.get("success") and payload.get("okay")):
            raise RequestFailedError(
                f"useServerBuff failed (failure_reason={payload.get('failure_reason')!r})"
            )
        return BlacksmithResult(
            actions=list(payload.get("actions") or []),
            buffs_remaining=payload.get("buffs_remaining"),
        )


def build_client(timeout: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "icr/0.1 (+https://github.com/)"},
    )
