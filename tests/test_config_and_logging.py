from __future__ import annotations

import logging

import pytest

from icr.config import Settings
from icr.logging_setup import RedactionFilter, _UrlQueryRedactor, register_secret


def make_record(msg: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord("t", logging.INFO, __file__, 1, msg, args, None)


# --------------------------------------------------------------------------
# config validation
# --------------------------------------------------------------------------


def test_loopback_web_needs_no_token() -> None:
    Settings(web_enabled=True, web_host="127.0.0.1", web_auth_token="")


def test_localhost_counts_as_loopback() -> None:
    Settings(web_enabled=True, web_host="localhost", web_auth_token="")


def test_exposed_web_without_token_is_refused() -> None:
    """The UI can read and write account credentials, so this must not start."""
    with pytest.raises(ValueError, match="ICR_WEB_AUTH_TOKEN"):
        Settings(web_enabled=True, web_host="0.0.0.0", web_auth_token="")


def test_exposed_web_with_token_is_allowed() -> None:
    Settings(web_enabled=True, web_host="0.0.0.0", web_auth_token="s3cret")


def test_insecure_bind_flag_permits_containerised_bind() -> None:
    """Containers must bind 0.0.0.0 even when the published port is loopback."""
    Settings(
        web_enabled=True, web_host="0.0.0.0", web_auth_token="", web_insecure_bind=True
    )


def test_insecure_bind_error_names_the_escape_hatch() -> None:
    with pytest.raises(ValueError, match="ICR_WEB_INSECURE_BIND"):
        Settings(web_enabled=True, web_host="0.0.0.0", web_auth_token="")


def test_discord_enabled_without_credentials_is_refused() -> None:
    with pytest.raises(ValueError, match="ICR_DISCORD_BOT_TOKEN"):
        Settings(discord_enabled=True, discord_bot_token="", discord_mirror_channel_id="")


def test_secrets_lists_only_configured_values() -> None:
    settings = Settings(
        discord_enabled=True,
        discord_bot_token="bot-token",
        discord_mirror_channel_id="1",
        web_auth_token="",
    )
    assert settings.secrets() == ["bot-token"]


# --------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------


def test_registered_secret_is_scrubbed() -> None:
    register_secret("super-secret-hash")
    record = make_record("account hash is super-secret-hash ok")
    RedactionFilter().filter(record)
    assert "super-secret-hash" not in record.getMessage()
    assert "***" in record.getMessage()


def test_secret_passed_as_format_arg_is_scrubbed() -> None:
    """Redacting only `record.msg` would miss `log.info("t=%s", token)`."""
    register_secret("arg-secret-value")
    record = make_record("token=%s", "arg-secret-value")
    RedactionFilter().filter(record)
    assert "arg-secret-value" not in record.getMessage()


def test_short_values_are_not_registered() -> None:
    """A three-character 'secret' would corrupt ordinary log lines."""
    register_secret("abc")
    record = make_record("the abc of logging")
    RedactionFilter().filter(record)
    assert record.getMessage() == "the abc of logging"


def test_unregistered_hash_in_query_string_is_blanked() -> None:
    """Second line of defence: a hash we never registered still must not leak."""
    record = make_record("GET http://ps7/post.php?user_id=1&hash=neverseenbefore&code=X")
    _UrlQueryRedactor().filter(record)
    message = record.getMessage()
    assert "neverseenbefore" not in message
    assert "code=X" in message  # non-secret params survive
