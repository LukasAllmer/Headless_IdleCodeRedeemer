"""Credential extraction from the game's support URL.

Ported from the extension's `options.ts::parseSupportUrl`. Opening the in-game
support page puts `user_id` and `device_hash` in the browser's address bar; that
URL is how you get credentials in the first place, so pasting it whole is far
less error-prone than transcribing two opaque strings.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse


class SupportUrlError(ValueError):
    pass


def parse_support_url(url: str) -> tuple[str, str]:
    """Return `(user_id, user_hash)` from a game support URL."""
    try:
        parsed = urlparse(url.strip())
    except ValueError as exc:
        raise SupportUrlError(f"Could not parse {url!r} as a URL.") from exc

    if not parsed.scheme or not parsed.netloc:
        raise SupportUrlError(
            "That does not look like a URL. Copy the whole address from the "
            "browser's address bar, including the https:// prefix."
        )

    params = parse_qs(parsed.query)
    user_id = _first(params, "user_id")
    user_hash = _first(params, "device_hash")

    if not user_id or not user_hash:
        missing = ", ".join(
            name for name, value in (("user_id", user_id), ("device_hash", user_hash))
            if not value
        )
        raise SupportUrlError(
            f"The URL is missing {missing}. Make sure you copied the address of "
            "the in-game support page, not the page it redirects to."
        )

    return user_id, user_hash


def _first(params: dict[str, list[str]], key: str) -> str | None:
    values = params.get(key)
    return values[0] if values and values[0] else None
