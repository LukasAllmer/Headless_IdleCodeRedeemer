"""Game API exceptions.

The two retry signals -- `SwitchServer` and `OutdatedInstanceId` -- are
exceptions rather than return values. In the original TypeScript they were
`GenericResponse` variants that every caller had to remember to check for, which
is exactly how the "retry once then abort everything" bug in `service_worker.ts`
came about. As exceptions the orchestration layer handles them in one place.
"""

from __future__ import annotations


class GameApiError(Exception):
    """Base class for anything the game server tells us went wrong."""


class SwitchServerError(GameApiError):
    """The account has been moved to a different play server."""

    def __init__(self, new_server: str) -> None:
        super().__init__(f"Server moved to {new_server}")
        self.new_server = new_server


class OutdatedInstanceIdError(GameApiError):
    """`instance_id` is stale; refresh it via `getuserdetails` and retry."""


class InvalidCredentialsError(GameApiError):
    """`user_id`/`hash` were rejected. Not retryable without user intervention."""


class InsufficientCurrencyError(GameApiError):
    """Not enough gems/gold for a purchase."""


class RequestFailedError(GameApiError):
    """Transport failure, non-2xx, unparseable body, or an unrecognised
    `failure_reason`. Retryable."""
