from __future__ import annotations

import pytest

from icr.support_url import SupportUrlError, parse_support_url


def test_parses_credentials() -> None:
    url = "https://idlechampions.com/support?user_id=12345&device_hash=abcdef"
    assert parse_support_url(url) == ("12345", "abcdef")


def test_tolerates_extra_params_and_whitespace() -> None:
    url = "  https://example.com/s?lang=en&user_id=1&device_hash=h&v=2  "
    assert parse_support_url(url) == ("1", "h")


def test_rejects_non_url() -> None:
    with pytest.raises(SupportUrlError, match="does not look like a URL"):
        parse_support_url("user_id=1&device_hash=h")


def test_names_the_missing_parameter() -> None:
    with pytest.raises(SupportUrlError, match="device_hash"):
        parse_support_url("https://example.com/s?user_id=12345")


def test_empty_values_count_as_missing() -> None:
    with pytest.raises(SupportUrlError, match="user_id"):
        parse_support_url("https://example.com/s?user_id=&device_hash=h")
