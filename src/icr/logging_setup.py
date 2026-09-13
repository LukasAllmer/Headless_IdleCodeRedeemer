"""Logging configuration with mandatory secret redaction.

Two things matter here beyond the usual handler wiring:

* **Redaction.** Account hashes and API tokens must never land in a log file.
  `RedactionFilter` rewrites known secrets out of every record, and
  `register_secret` lets the account layer add hashes as they are loaded.
* **Run correlation.** `run_id` is carried in a `contextvars.ContextVar` so
  every line emitted during one redeem run shares an id and is greppable.
"""

from __future__ import annotations

import contextvars
import logging
import logging.handlers
import re
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_run_id: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="-")
_secrets: set[str] = set()

REDACTED = "***"
_MIN_SECRET_LENGTH = 6
"""Below this, a "secret" is more likely to appear as an innocent substring of
ordinary text than to be worth redacting."""


def register_secret(*values: str | None) -> None:
    """Add values to be scrubbed from all future log records."""
    for value in values:
        if value and len(value) >= _MIN_SECRET_LENGTH:
            _secrets.add(value)


def current_run_id() -> str:
    return _run_id.get()


@contextmanager
def run_context(run_id: str | None = None) -> Iterator[str]:
    """Tag every log record emitted inside this block with a shared id."""
    value = run_id or uuid.uuid4().hex[:8]
    token = _run_id.set(value)
    try:
        yield value
    finally:
        _run_id.reset(token)


class RunIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = _run_id.get()
        return True


class RedactionFilter(logging.Filter):
    """Scrub registered secrets from the message and its arguments.

    Formatting happens here rather than in the handler so that secrets passed as
    `%s` arguments are caught too -- redacting only `record.msg` would miss
    `log.info("token=%s", token)`.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not _secrets:
            return True
        message = record.getMessage()
        redacted = _redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def _redact(text: str) -> str:
    for secret in _secrets:
        if secret in text:
            text = text.replace(secret, REDACTED)
    return text


class _UrlQueryRedactor(logging.Filter):
    """Belt-and-braces for game-server URLs.

    Request URLs carry `hash=` and `user_id=` in the query string. Registered
    secrets already cover the hashes we know about, but a hash we failed to
    register would otherwise leak in full. Blank the values structurally.
    """

    _PATTERN = re.compile(r"(?i)\b(hash|user_id|token|instance_id)=([^&\s\"']+)")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "=" not in message:
            return True
        redacted = self._PATTERN.sub(rf"\1={REDACTED}", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


FORMAT = "%(asctime)s %(levelname)-8s %(name)-22s [%(run_id)s] %(message)s"


def setup_logging(
    level: str = "INFO",
    log_file: Path | None = None,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """Configure the root logger. Safe to call more than once."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    root.setLevel(level.upper())
    formatter = logging.Formatter(FORMAT)
    filters = [RunIdFilter(), RedactionFilter(), _UrlQueryRedactor()]

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    handlers: list[logging.Handler] = [stream]

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        rotating.setFormatter(formatter)
        handlers.append(rotating)

    for handler in handlers:
        for f in filters:
            handler.addFilter(f)
        root.addHandler(handler)

    # httpx logs the full request URL at INFO, which includes credentials. The
    # redactors below would catch it, but there is no reason to generate it.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
