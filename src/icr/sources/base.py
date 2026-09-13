"""Code source framework.

A source is a producer that does one thing: hand back codes it has found. It
never redeems, never touches accounts, and never decides what is new -- the
`codes` table's uniqueness constraint handles deduplication. Adding a source
means one new module and one registry entry.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Protocol

from icr.config import Settings
from icr.db import repo

log = logging.getLogger(__name__)

#: Ported verbatim from the extension's `src/inject/inject.ts`. Matches a 12- or
#: 16-character run from the game's code alphabet, optionally dash-separated.
#: It is deliberately loose and will occasionally match ordinary prose; a false
#: positive costs one API call and is recorded as `invalid`, which is cheaper
#: than missing a real code.
CODE_PATTERN = re.compile(r"(?:[A-Z0-9*!@#$%^&*]-?){12}(?:(?:[A-Z0-9*!@#$%^&*]-?){4})?")


def extract_codes(text: str) -> list[str]:
    """Pull every code out of a block of text, normalised and de-duplicated.

    Unlike the extension -- which took only the first match per message -- this
    returns all of them, since a single announcement often carries several.
    """
    seen: dict[str, None] = {}
    for match in CODE_PATTERN.findall(text.upper()):
        code = match.replace("-", "")
        if len(code) in (12, 16):
            seen.setdefault(code, None)
    return list(seen)


class SourceError(Exception):
    """A source failed in a way that is understood and actionable.

    Raised for misconfiguration -- a bad token, a missing permission, a wrong
    channel id -- where the message alone tells the user what to fix. These are
    logged without a traceback; anything else gets the full stack, because an
    unexpected failure is worth the noise.
    """


@dataclass(slots=True)
class DiscoveredCode:
    code: str
    source_ref: str | None = None
    note: str | None = None

    account_id: int | None = None
    """Which account this code belongs to, or None if anyone may redeem it.

    Set by sources whose codes are personal -- a newsletter code mailed to one
    subscriber is single-use, so offering it to the other accounts would burn it
    on the wrong one.
    """


class CodeSource(Protocol):
    name: str

    def enabled(self, settings: Settings) -> bool: ...

    def position(self, conn: sqlite3.Connection) -> str | None:
        """How far this source has read, for `icr source list`. None if it has
        not read anything yet, or keeps no position at all."""
        ...

    def reset(self, conn: sqlite3.Connection) -> None:
        """Forget the position so the next poll re-scans."""
        ...

    async def poll(self, conn: sqlite3.Connection, settings: Settings) -> list[DiscoveredCode]:
        """Return codes found since the last poll.

        Implementations persist their own cursor in the `kv` table so that a
        restart does not re-scan from the beginning.
        """
        ...


@dataclass
class PollResult:
    source: str
    found: int = 0
    added: int = 0
    error: str | None = None

    def describe(self) -> str:
        if self.error:
            return f"{self.source}: failed ({self.error})"
        return f"{self.source}: {self.found} found, {self.added} new"

    def brief(self) -> str:
        """The compact form used in the per-cycle summary line."""
        return f"{self.source} failed" if self.error else f"{self.source} {self.found}/{self.added}"


def describe_cycle(results: list[PollResult]) -> str:
    """One line accounting for every source a cycle touched.

    A source that finds nothing logs nothing of its own -- an unchanged Discord
    cursor or a 304 from a code list are both debug-level non-events -- so at
    INFO the only evidence a cycle ran at all came from whichever source happened
    to be chatty. This says what each one did, including the quiet ones.

    Built from the results rather than from `REGISTRY`, so it covers however many
    sources are enabled without knowing their names.
    """
    if not results:
        return "no sources enabled, nothing polled"
    found = sum(r.found for r in results)
    added = sum(r.added for r in results)
    failed = sum(1 for r in results if r.error)
    tail = f", {failed} failed" if failed else ""
    detail = ", ".join(r.brief() for r in results)
    return (
        f"{len(results)} source(s), {found} code(s) found, {added} new{tail} "
        f"-- {detail} (found/new)"
    )


REGISTRY: dict[str, CodeSource] = {}


def register(source: CodeSource) -> CodeSource:
    REGISTRY[source.name] = source
    return source


async def poll_source(
    source: CodeSource, conn: sqlite3.Connection, settings: Settings
) -> PollResult:
    result = PollResult(source=source.name)
    try:
        discovered = await source.poll(conn, settings)
    except SourceError as exc:
        # Actionable misconfiguration: the message is the whole story.
        log.error("Source %s: %s", source.name, exc)
        result.error = str(exc)
        return result
    except Exception as exc:  # a broken source must not take the service down
        log.exception("Source %s failed to poll", source.name)
        result.error = str(exc)
        return result

    result.found = len(discovered)
    for item in discovered:
        stored, was_new = repo.add_code(
            conn,
            item.code,
            source=source.name,
            source_ref=item.source_ref,
            note=item.note,
            account_id=item.account_id,
        )
        if was_new:
            result.added += 1
            scope = f" for account {item.account_id}" if item.account_id else ""
            log.info("New code from %s: %s%s", source.name, item.code, scope)
        elif item.account_id is not None and stored.account_id != item.account_id:
            # Two mailboxes on different accounts received the same string, or a
            # public source got there first. The first scope wins; say so rather
            # than silently redeeming it somewhere the user did not expect.
            log.warning(
                "Code %s arrived from %s for account %s, but is already on file "
                "as %s. Leaving it as it is.",
                item.code,
                source.name,
                item.account_id,
                f"account {stored.account_id}'s" if stored.account_id else "public",
            )
    return result


async def poll_all(conn: sqlite3.Connection, settings: Settings) -> list[PollResult]:
    results = []
    for source in REGISTRY.values():
        if not source.enabled(settings):
            log.debug("Source %s is disabled, skipping.", source.name)
            continue
        results.append(await poll_source(source, conn, settings))
    return results
