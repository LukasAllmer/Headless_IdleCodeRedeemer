"""Shared machinery for sources that read a public web page.

Both community code lists work the same way: fetch one URL, pull codes out of
it, hand them over. The codes are public and multi-use, so they are never scoped
to an account, and heavy overlap with Discord (and with each other) is expected
and harmless -- the `codes` uniqueness constraint absorbs it.

Every code is returned on every poll rather than only the ones that look new.
Deciding what is new is the database's job, and a source that withholds codes it
can see would never heal a database restored from an older backup. The content
digest here only chooses a log level.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from typing import ClassVar

import httpx

from icr.config import Settings
from icr.db import repo, utcnow
from icr.sources.base import DiscoveredCode, SourceError

log = logging.getLogger(__name__)

#: Identifies the tool to the sites it reads, with a way to find out what it is.
#: Both are small community sites; polling them anonymously and often is rude.
USER_AGENT = "IdleCodeRedeemer/0.1"


@dataclass(slots=True)
class Page:
    text: str
    etag: str | None


async def fetch(
    url: str,
    *,
    timeout: float,
    etag: str | None = None,
    params: dict[str, str] | None = None,
) -> Page | None:
    """GET a page. Returns None when the server says it has not changed."""
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}
    if etag:
        headers["If-None-Match"] = etag

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(url, headers=headers, params=params)
    except httpx.HTTPError as exc:
        raise SourceError(f"Could not reach {url} -- {exc}") from exc

    if response.status_code == httpx.codes.NOT_MODIFIED:
        return None
    if response.status_code == httpx.codes.FORBIDDEN:
        raise SourceError(
            f"{url} returned 403. The site is refusing this client; it may have "
            "added a bot check."
        )
    if response.status_code >= 400:
        raise SourceError(f"{url} returned HTTP {response.status_code}.")

    return Page(text=response.text, etag=response.headers.get("ETag"))


class WebCodeList:
    """Base for a source that is one page listing public codes.

    Subclasses provide `name`, `url`, the settings flag, and `parse`.
    """

    name: str = ""
    url: str = ""
    params: ClassVar[dict[str, str] | None] = None

    def enabled(self, settings: Settings) -> bool:
        raise NotImplementedError

    def parse(self, body: str) -> list[DiscoveredCode]:
        raise NotImplementedError

    # -- cursor ---------------------------------------------------------

    @property
    def _prefix(self) -> str:
        return f"web:{self.name}:"

    def position(self, conn: sqlite3.Connection) -> str | None:
        seen = repo.kv_get(conn, self._prefix + "seen")
        count = repo.kv_get(conn, self._prefix + "count")
        if not seen:
            return None
        return f"{count or '?'} code(s), last changed {seen}"

    def reset(self, conn: sqlite3.Connection) -> None:
        repo.kv_delete_prefix(conn, self._prefix)

    # -- polling --------------------------------------------------------

    async def poll(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> list[DiscoveredCode]:
        etag = repo.kv_get(conn, self._prefix + "etag")
        page = await fetch(
            self.url,
            timeout=settings.http_timeout_seconds,
            etag=etag,
            params=self.params,
        )
        if page is None:
            log.debug("%s: unchanged since last poll (304).", self.name)
            return []

        found = self.parse(page.text)
        if not found:
            # The page loaded but nothing in it looked like a code list. Either
            # the layout changed or something is standing in front of the page.
            raise SourceError(
                f"{self.url} loaded but contained no codes. The page layout has "
                "probably changed and this source needs updating."
            )

        if page.etag:
            repo.kv_set(conn, self._prefix + "etag", page.etag)

        digest = hashlib.sha256(
            "\n".join(sorted(item.code for item in found)).encode()
        ).hexdigest()
        if digest == repo.kv_get(conn, self._prefix + "digest"):
            log.debug("%s: same %d code(s) as last poll.", self.name, len(found))
        else:
            repo.kv_set(conn, self._prefix + "digest", digest)
            repo.kv_set(conn, self._prefix + "seen", utcnow())
            repo.kv_set(conn, self._prefix + "count", str(len(found)))
            log.info("%s: %d code(s) listed, page changed.", self.name, len(found))

        return found
