"""The Idle Champions wiki's Combinations page.

<https://idlechampions.fandom.com/wiki/Combinations> keeps the long-lived public
codes -- the ones that stay valid for months, which the Discord announcements
scroll past.

This reads the **MediaWiki API**, not the rendered page. Fandom serves 403 to
anything that does not look like a browser, so scraping the HTML does not work
at all; the API is public, returns the source wikitext, and is far more stable
than the site's markup.

Only `{{combination|CODE}}` templates are read, which matters for correctness
rather than convenience: the page also contains a bare code in prose for the
Crusaders of the Lost Idols sunsetting, and that one is restricted to accounts
with a CotLI history. Scanning the whole page would queue it for everybody.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, ClassVar

from icr.config import Settings
from icr.sources.base import DiscoveredCode, SourceError, extract_codes, register
from icr.sources.web import WebCodeList

log = logging.getLogger(__name__)

API = "https://idlechampions.fandom.com/api.php"
PAGE = "Combinations"
PAGE_URL = f"https://idlechampions.fandom.com/wiki/{PAGE}"

#: The template the wiki wraps every listed code in.
_COMBINATION = re.compile(r"\{\{\s*combination\s*\|([^}|]+)\}\}", re.IGNORECASE)


class FandomWikiSource(WebCodeList):
    name = "fandom"
    url = API
    params: ClassVar[dict[str, str]] = {
        "action": "parse",
        "page": PAGE,
        "prop": "wikitext",
        "format": "json",
        # v1 returns the wikitext as {"*": "..."}; v2 gives a plain string.
        "formatversion": "2",
    }

    def enabled(self, settings: Settings) -> bool:
        return settings.fandom_enabled

    def parse(self, body: str) -> list[DiscoveredCode]:
        wikitext = _wikitext(body)
        discovered: list[DiscoveredCode] = []
        seen: set[str] = set()

        for raw in _COMBINATION.findall(wikitext):
            for code in extract_codes(raw):
                if code in seen:
                    continue
                seen.add(code)
                discovered.append(
                    DiscoveredCode(
                        code=code,
                        source_ref=f"fandom:{PAGE}",
                        note=f"listed on {PAGE_URL}",
                    )
                )
        return discovered


def _wikitext(body: str) -> str:
    try:
        payload: Any = json.loads(body)
    except ValueError as exc:
        raise SourceError(f"{API} returned something that is not JSON.") from exc

    if isinstance(payload, dict) and "error" in payload:
        detail = payload["error"].get("info", payload["error"])
        raise SourceError(f"The wiki API rejected the request: {detail}")

    try:
        return str(payload["parse"]["wikitext"])
    except (KeyError, TypeError) as exc:
        raise SourceError(
            f"The wiki API response had no wikitext for {PAGE}. The page may have "
            "been renamed."
        ) from exc


register(FandomWikiSource())
