"""Play-server and instance-id lifecycle, shared by every authenticated call.

Every game endpoint can answer "you're on the wrong server" or "your instance id
is stale" instead of doing what was asked. Handling that per call site is how
the original extension ended up retrying inconsistently, so it lives here once:
`GameSession.call` runs an operation and transparently recovers from both.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Awaitable, Callable
from typing import TypeVar

from icr.db import repo
from icr.game.api import IdleChampionsApi
from icr.game.errors import (
    OutdatedInstanceIdError,
    RequestFailedError,
    SwitchServerError,
)
from icr.models import Account

log = logging.getLogger(__name__)

KV_PLAY_SERVER = "play_server"

#: Recoveries allowed per operation. Two plus the original call covers
#: "switched server, then needed a fresh instance id", which is a real sequence
#: for a freshly migrated account. More than that means something is wrong.
MAX_RECOVERIES = 3

T = TypeVar("T")

#: An operation receives the current server and instance id, and returns a
#: coroutine. It may be called more than once, so it must be side-effect free
#: beyond the request itself.
Operation = Callable[[str, str], Awaitable[T]]


class GameSession:
    def __init__(self, conn: sqlite3.Connection, api: IdleChampionsApi) -> None:
        self._conn = conn
        self._api = api
        self._server: str | None = None

    async def server(self) -> str:
        """The current play server, resolved once and cached in the database."""
        if self._server:
            return self._server
        cached = repo.kv_get(self._conn, KV_PLAY_SERVER)
        if cached:
            self._server = cached
            return cached
        server = await self._api.get_play_server()
        self.set_server(server)
        return server

    def set_server(self, server: str) -> None:
        if server != self._server:
            log.info("Using play server %s", server)
        self._server = server
        repo.kv_set(self._conn, KV_PLAY_SERVER, server)

    async def call(self, account: Account, operation: Operation[T]) -> T:
        """Run `operation`, recovering from server switches and stale instance ids.

        `InvalidCredentialsError` is deliberately *not* caught -- it needs a
        human, and the caller decides how far to unwind.
        """
        server = await self.server()

        for _ in range(MAX_RECOVERIES):
            try:
                return await operation(server, account.instance_id or "")
            except SwitchServerError as exc:
                log.info("Account %s: play server moved, retrying.", account.name)
                self.set_server(exc.new_server)
                server = exc.new_server
            except OutdatedInstanceIdError:
                log.info("Account %s: instance id stale, refreshing.", account.name)
                server = await self.refresh_instance_id(account)

        raise RequestFailedError(
            f"Gave up after {MAX_RECOVERIES} recovery attempts for account {account.name}"
        )

    async def refresh_instance_id(self, account: Account) -> str:
        """Fetch and persist a fresh instance id. Returns the server to use next."""
        server = await self.server()

        for _ in range(MAX_RECOVERIES):
            try:
                details = await self._api.get_user_details(
                    server=server,
                    user_id=account.user_id,
                    user_hash=account.user_hash,
                )
            except SwitchServerError as exc:
                self.set_server(exc.new_server)
                server = exc.new_server
                continue

            account.instance_id = details.instance_id
            repo.update_instance_id(self._conn, account.id, details.instance_id)
            log.debug("Account %s: instance id refreshed.", account.name)
            return server

        raise RequestFailedError("Could not refresh instance id: server kept redirecting")
