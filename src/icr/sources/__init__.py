"""Code sources.

Importing this package registers every built-in source. Manual entry is not a
polling source -- it writes to `codes` directly from the CLI and web UI -- so it
has no module here.
"""

# Importing each module registers its source. Order here is poll order.
from icr.sources import (  # noqa: F401
    discord_follow,
    email_inbox,
    fandom_wiki,
    incendar,
)
from icr.sources.base import (
    REGISTRY,
    CodeSource,
    DiscoveredCode,
    PollResult,
    SourceError,
    describe_cycle,
    extract_codes,
    poll_all,
    poll_source,
    register,
)

__all__ = [
    "REGISTRY",
    "CodeSource",
    "DiscoveredCode",
    "PollResult",
    "SourceError",
    "describe_cycle",
    "extract_codes",
    "poll_all",
    "poll_source",
    "register",
]
