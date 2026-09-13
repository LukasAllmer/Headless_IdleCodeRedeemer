"""Discord source: polls a channel that follows the Idle Champions #combinations
announcement channel.

`#combinations` is an Announcement channel, followed into a personal guild, so
Discord cross-posts every new message there via webhook. A plain bot in that
guild can read the mirror over REST -- no gateway connection, no `discord.py`,
and restart-safe because the cursor lives in the database.

The bot needs the **Message Content Intent** enabled in the Developer Portal.
Without it `content` comes back empty even over REST, and this source will
silently find nothing.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any

import httpx

from icr.config import Settings
from icr.db import repo
from icr.sources.base import DiscoveredCode, SourceError, extract_codes, register

log = logging.getLogger(__name__)

API_BASE = "https://discord.com/api/v10"
KV_CURSOR = "discord:last_message_id"

_MAX_RATE_LIMIT_RETRIES = 3


class DiscordFollowSource:
    name = "discord"

    def enabled(self, settings: Settings) -> bool:
        return settings.discord_enabled

    def position(self, conn: sqlite3.Connection) -> str | None:
        return repo.kv_get(conn, KV_CURSOR)

    def reset(self, conn: sqlite3.Connection) -> None:
        repo.kv_delete(conn, KV_CURSOR)

    async def poll(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> list[DiscoveredCode]:
        cursor = repo.kv_get(conn, KV_CURSOR)
        messages = await self._fetch(settings, after=cursor)

        if not messages:
            log.debug("Discord: no new messages since %s", cursor or "(start)")
            return []

        # The API returns newest-first; process oldest-first so the cursor
        # advances monotonically even if we bail partway through.
        messages.sort(key=lambda m: int(m["id"]))

        discovered: list[DiscoveredCode] = []
        for message in messages:
            for code in extract_codes(_message_text(message)):
                discovered.append(
                    DiscoveredCode(
                        code=code,
                        source_ref=str(message["id"]),
                        note=_note_for(message),
                    )
                )

        newest = str(messages[-1]["id"])
        repo.kv_set(conn, KV_CURSOR, newest)
        log.info(
            "Discord: scanned %d message(s), found %d code(s), cursor now %s",
            len(messages),
            len(discovered),
            newest,
        )
        return discovered

    async def _fetch(self, settings: Settings, *, after: str | None) -> list[dict[str, Any]]:
        params: dict[str, str | int] = {"limit": settings.discord_fetch_limit}
        if after:
            params["after"] = after

        url = f"{API_BASE}/channels/{settings.discord_mirror_channel_id}/messages"
        headers = {
            "Authorization": f"Bot {settings.discord_bot_token}",
            "User-Agent": "DiscordBot (https://github.com/, 0.1)",
        }

        async with httpx.AsyncClient(timeout=settings.http_timeout_seconds) as client:
            for attempt in range(_MAX_RATE_LIMIT_RETRIES):
                response = await client.get(url, params=params, headers=headers)

                if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
                    wait = _retry_after(response)
                    log.warning(
                        "Discord rate limited, waiting %.1fs (attempt %d/%d)",
                        wait,
                        attempt + 1,
                        _MAX_RATE_LIMIT_RETRIES,
                    )
                    await asyncio.sleep(wait)
                    continue

                if response.status_code == httpx.codes.UNAUTHORIZED:
                    raise SourceError(
                        "Discord rejected the bot token (401). Check ICR_DISCORD_BOT_TOKEN."
                    )
                if response.status_code == httpx.codes.FORBIDDEN:
                    raise SourceError(
                        "Discord returned 403 for the mirror channel. The bot needs "
                        "'View Channel' and 'Read Message History' on "
                        f"channel {settings.discord_mirror_channel_id}."
                    )
                if response.status_code == httpx.codes.NOT_FOUND:
                    raise SourceError(
                        f"Discord channel {settings.discord_mirror_channel_id} not found. "
                        "Check ICR_DISCORD_MIRROR_CHANNEL_ID."
                    )
                response.raise_for_status()

                payload = response.json()
                if not isinstance(payload, list):
                    raise SourceError("Discord returned an unexpected payload shape")
                return payload

        raise SourceError("Discord stayed rate limited across every retry")


def _retry_after(response: httpx.Response) -> float:
    try:
        return float(response.json().get("retry_after", 5.0))
    except (ValueError, AttributeError):
        return 5.0


def _message_text(message: dict[str, Any]) -> str:
    """Codes appear in the message body, but announcement posts often put them
    in an embed instead, so scan both."""
    parts = [str(message.get("content") or "")]
    for embed in message.get("embeds") or []:
        for key in ("title", "description"):
            if value := embed.get(key):
                parts.append(str(value))
        for field in embed.get("fields") or []:
            for key in ("name", "value"):
                if value := field.get(key):
                    parts.append(str(value))
    return "\n".join(parts)


def _note_for(message: dict[str, Any]) -> str:
    author = (message.get("author") or {}).get("username", "unknown")
    timestamp = message.get("timestamp", "")
    return f"discord message from {author} at {timestamp}"


register(DiscordFollowSource())
