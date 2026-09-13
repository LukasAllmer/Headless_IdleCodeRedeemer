"""Incendar's community code tracker.

<https://incendar.com/idlechampions_codes.php> keeps a table of active
combination codes with the chest each one gives. The codes are public and
multi-use, and overlap heavily with Discord and the wiki -- which costs nothing,
because a code already on file is ignored.

The codes are not in the page text. Each one sits in the `value` attribute of a
readonly `<input class="code-box">`, which is what the site's click-to-copy
behaviour reads. So this parses the input tags rather than the rendered text; a
text scrape of this page finds nothing at all.
"""

from __future__ import annotations

import html
import logging
import re

from icr.config import Settings
from icr.sources.base import DiscoveredCode, extract_codes, register
from icr.sources.web import WebCodeList

log = logging.getLogger(__name__)

URL = "https://incendar.com/idlechampions_codes.php"

_ROW = re.compile(r"<tr\b.*?</tr>", re.IGNORECASE | re.DOTALL)
_CODE_INPUT = re.compile(
    r"<input[^>]*\bclass=['\"][^'\"]*\bcode-box\b[^'\"]*['\"][^>]*>",
    re.IGNORECASE,
)
_ATTR = r"\b{}=['\"]([^'\"]*)['\"]"
_VALUE = re.compile(_ATTR.format("value"), re.IGNORECASE)
_ID = re.compile(_ATTR.format("id"), re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")


class IncendarSource(WebCodeList):
    name = "incendar"
    url = URL

    def enabled(self, settings: Settings) -> bool:
        return settings.incendar_enabled

    def parse(self, body: str) -> list[DiscoveredCode]:
        discovered: list[DiscoveredCode] = []
        seen: set[str] = set()

        for row in _ROW.findall(body):
            for tag in _CODE_INPUT.findall(row):
                value = _VALUE.search(tag)
                if not value:
                    continue
                record = _ID.search(tag)
                for code in extract_codes(html.unescape(value.group(1))):
                    if code in seen:
                        continue
                    seen.add(code)
                    discovered.append(
                        DiscoveredCode(
                            code=code,
                            source_ref=f"incendar:{record.group(1)}" if record else URL,
                            note=_row_note(row),
                        )
                    )
        return discovered


def _row_note(row: str) -> str:
    """The rest of the row, which carries the date and what the code gives."""
    text = html.unescape(_TAG.sub(" ", row))
    return " ".join(text.split())[:200] or "incendar code list"


register(IncendarSource())
