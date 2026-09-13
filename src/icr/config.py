"""Runtime configuration, loaded from environment and `.env`.

Everything the service needs lives here *except* account credentials, which are
stored in the database (see `PLAN.md` §4): `instance_id` is mutable state the
service rewrites itself, and indexed environment variables get unpleasant past
two accounts.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ICR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- storage -----------------------------------------------------------
    db_path: Path = Field(default=Path("icr.sqlite3"))

    # --- logging -----------------------------------------------------------
    log_level: str = "INFO"
    log_file: Path | None = None
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 5

    # --- game server pacing ------------------------------------------------
    request_delay_seconds: float = Field(default=2.0, ge=0.0)
    """Minimum gap between *any* two game-server calls, applied globally so
    that N accounts do not multiply the request rate."""

    max_redeem_attempts: int = Field(default=3, ge=1)
    http_timeout_seconds: float = Field(default=30.0, gt=0.0)

    # --- scheduling --------------------------------------------------------
    source_poll_interval_seconds: int = Field(default=300, ge=30)
    """The only timer. Redemption has none of its own -- it runs after a poll
    if that left anything outstanding, and whenever it is triggered by hand."""

    # --- discord source ----------------------------------------------------
    discord_enabled: bool = False
    discord_bot_token: str = ""
    discord_mirror_channel_id: str = ""
    discord_fetch_limit: int = Field(default=100, ge=1, le=100)

    # --- public code lists -------------------------------------------------
    # On by default: both are public pages needing no credentials, and their
    # codes are multi-use, so there is nothing to set up and nothing to lose.
    incendar_enabled: bool = True
    """<https://incendar.com/idlechampions_codes.php>"""

    fandom_enabled: bool = True
    """<https://idlechampions.fandom.com/wiki/Combinations>, read via its API."""

    # --- email source ------------------------------------------------------
    email_enabled: bool = False

    email_senders: str = "newsletters@codenameentertainment.com"
    """Comma-separated addresses to accept mail from; empty scans everything.

    The code pattern is deliberately loose and `extract_codes` upper-cases before
    matching, so an unfiltered mailbox turns every 12-character token in every
    newsletter on earth into a redemption attempt. Restricting by sender is what
    keeps the false-positive rate survivable.
    """

    email_initial_scan_days: int = Field(default=0, ge=0)
    """How far back the first read of a folder reaches. 0 means all of it."""

    email_rescan_days: int = Field(default=2, ge=0)
    """Recovery window for a folder whose UID position stopped meaning anything.

    Steady-state polling does not use this: read position is a per-folder UID, so
    an ordinary poll asks the server for messages after the last one it read and
    re-reads nothing. This is the fallback window for the two cases where that
    number is void -- the server bumped a folder's UIDVALIDITY, or a mailbox is
    being carried over from the older date-based cursor. Codes already extracted
    are still on file in either case, so only recent mail needs covering.
    """

    email_timeout_seconds: float = Field(default=60.0, gt=0.0)
    email_max_messages_per_folder: int = Field(default=200, ge=1)

    email_oauth_client_id: str = ""
    """Default Azure application (client) id for Microsoft mailboxes. Each
    mailbox may override it; one registration usually covers them all."""

    email_oauth_tenant: str = "common"

    # --- web ---------------------------------------------------------------
    web_enabled: bool = True
    web_host: str = "127.0.0.1"
    web_port: int = Field(default=8787, ge=1, le=65535)
    web_auth_token: str = ""

    web_insecure_bind: bool = False
    """Permit an unauthenticated bind to a non-loopback address.

    Exists for containers: a process inside Docker must listen on 0.0.0.0 to be
    reachable at all, even when the published port is `127.0.0.1:8787:8787` and
    therefore no more exposed than a loopback bind. Setting this on a host
    without that outer restriction publishes an unauthenticated UI that can read
    and write account credentials.
    """

    @field_validator("log_file", mode="before")
    @classmethod
    def _blank_means_no_file(cls, value: object) -> object:
        """`ICR_LOG_FILE=` is how you turn file logging off.

        Without this it parses as `Path('.')` and logging setup dies trying to
        open a directory -- a confusing failure for an obvious thing to write.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> Settings:
        if self.discord_enabled and not (self.discord_bot_token and self.discord_mirror_channel_id):
            raise ValueError(
                "ICR_DISCORD_ENABLED is true but ICR_DISCORD_BOT_TOKEN and/or "
                "ICR_DISCORD_MIRROR_CHANNEL_ID are unset."
            )
        if (
            self.web_enabled
            and not self._host_is_loopback
            and not self.web_auth_token
            and not self.web_insecure_bind
        ):
            raise ValueError(
                f"ICR_WEB_HOST is {self.web_host!r}, which is not loopback, so "
                "ICR_WEB_AUTH_TOKEN must be set. Refusing to expose an unauthenticated "
                "UI that can read and write account credentials. If the bind is "
                "already restricted from outside the process -- a container whose "
                "port is published to loopback only -- set ICR_WEB_INSECURE_BIND=true."
            )
        return self

    @property
    def sender_list(self) -> list[str]:
        return [s.strip() for s in self.email_senders.split(",") if s.strip()]

    @property
    def _host_is_loopback(self) -> bool:
        if self.web_host == "localhost":
            return True
        try:
            return ipaddress.ip_address(self.web_host).is_loopback
        except ValueError:
            return False

    def secrets(self) -> list[str]:
        """Values that must never reach a log file. See `logging_setup`."""
        return [s for s in (self.discord_bot_token, self.web_auth_token) if s]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
