from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from icr.config import Settings, get_settings
from icr.db import connect, migrate
from icr.game.api import IdleChampionsApi

PLAY_SERVER = "http://ps7.idlechampions.com/~idledragons/post.php"


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's own `.env` out of the tests.

    `Settings` reads `.env` from the working directory, so without this a test
    that builds `Settings(...)` quietly inherits whatever the person running it
    has configured -- including a live Discord token -- and the suite passes or
    fails depending on whose machine it is on.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for key in list(os.environ):
        if key.startswith("ICR_"):
            monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "test.sqlite3")
    migrate(connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def api() -> Iterator[IdleChampionsApi]:
    """API client with pacing disabled so tests do not sleep."""
    client = httpx.AsyncClient()
    yield IdleChampionsApi(client, request_delay=0.0)


def ok(**payload: object) -> httpx.Response:
    base: dict[str, object] = {"success": True, "okay": True}
    return httpx.Response(200, json=base | payload)


def failure(reason: str, **payload: object) -> httpx.Response:
    return httpx.Response(200, json={"success": False, "failure_reason": reason} | payload)
