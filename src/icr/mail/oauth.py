"""Microsoft OAuth2 for IMAP, via the device code flow.

Microsoft disabled IMAP basic authentication, so an Outlook, Hotmail, Live or
Office 365 mailbox has to present an OAuth2 access token over XOAUTH2. Getting
one needs a user to sign in once; after that a refresh token kept in the
database renews access indefinitely without further interaction.

The device code flow is the right shape for a headless box: nothing listens on a
redirect URI, nothing opens a browser. The server prints a short code, the user
types it into microsoft.com/devicelogin on whatever machine they are sitting at,
and this end polls until it is approved.

This talks to the endpoints directly rather than pulling in `msal`. The flow is
three form posts, and `msal` drags in `cryptography` -- a compiled dependency
that would roughly double the container image for no gain here.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

AUTHORITY = "https://login.microsoftonline.com"

IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All"
#: `offline_access` is what makes Microsoft hand back a refresh token at all.
SCOPES = f"offline_access {IMAP_SCOPE}"

DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

#: Default IMAP endpoint for every consumer and Office 365 mailbox.
IMAP_HOST = "outlook.office365.com"
IMAP_PORT = 993


class OAuthError(Exception):
    """Something went wrong acquiring a token, phrased for the person who has to
    fix it rather than for a stack trace."""


@dataclass(slots=True)
class DeviceCodePrompt:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int

    def describe(self) -> str:
        return f"Open {self.verification_uri} and enter the code {self.user_code}"


@dataclass(slots=True)
class TokenPair:
    access_token: str
    refresh_token: str | None
    expires_in: int


def _token_url(tenant: str) -> str:
    return f"{AUTHORITY}/{tenant}/oauth2/v2.0/token"


async def _post(url: str, data: dict[str, str], timeout: float) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, data=data)
    try:
        payload = response.json()
    except ValueError as exc:
        raise OAuthError(
            f"Microsoft returned {response.status_code} with a non-JSON body."
        ) from exc
    if not isinstance(payload, dict):
        raise OAuthError("Microsoft returned an unexpected payload shape.")
    return payload


def _describe_failure(payload: dict[str, Any]) -> str:
    """Microsoft's `error_description` carries the AADSTS code and a readable
    sentence, so prefer it over the machine-readable `error` slug."""
    description = str(payload.get("error_description") or "").strip()
    if description:
        return description.splitlines()[0]
    return str(payload.get("error") or "unknown error")


async def begin_device_code(
    *, client_id: str, tenant: str = "common", timeout: float = 30.0
) -> DeviceCodePrompt:
    payload = await _post(
        f"{AUTHORITY}/{tenant}/oauth2/v2.0/devicecode",
        {"client_id": client_id, "scope": SCOPES},
        timeout,
    )
    if "device_code" not in payload:
        raise OAuthError(
            f"Microsoft refused to start the device code flow: {_describe_failure(payload)}. "
            "Check the client id, and that the app registration has 'Allow public "
            "client flows' enabled."
        )
    return DeviceCodePrompt(
        device_code=str(payload["device_code"]),
        user_code=str(payload["user_code"]),
        verification_uri=str(payload.get("verification_uri", "https://microsoft.com/devicelogin")),
        expires_in=int(payload.get("expires_in", 900)),
        interval=int(payload.get("interval", 5)),
    )


async def poll_device_code(
    prompt: DeviceCodePrompt, *, client_id: str, tenant: str = "common", timeout: float = 30.0
) -> TokenPair:
    """Wait for the user to approve the sign-in, then return the tokens.

    Blocks for up to `prompt.expires_in` seconds. `authorization_pending` is the
    normal state for almost all of them.
    """
    deadline = prompt.expires_in
    interval = max(prompt.interval, 1)
    waited = 0

    while waited < deadline:
        await asyncio.sleep(interval)
        waited += interval

        payload = await _post(
            _token_url(tenant),
            {
                "grant_type": DEVICE_CODE_GRANT,
                "client_id": client_id,
                "device_code": prompt.device_code,
            },
            timeout,
        )
        if "access_token" in payload:
            return _to_pair(payload)

        error = str(payload.get("error") or "")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "authorization_declined":
            raise OAuthError("The sign-in was declined.")
        if error == "expired_token":
            raise OAuthError("The code expired before it was entered. Run the command again.")
        raise OAuthError(f"Microsoft rejected the sign-in: {_describe_failure(payload)}")

    raise OAuthError("The code expired before it was entered. Run the command again.")


async def exchange_refresh_token(
    refresh_token: str, *, client_id: str, tenant: str = "common", timeout: float = 30.0
) -> TokenPair:
    """Trade the stored refresh token for a fresh access token.

    Microsoft rotates refresh tokens, so the returned one must be written back or
    the mailbox stops working when the old one is eventually invalidated.
    """
    payload = await _post(
        _token_url(tenant),
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
            "scope": SCOPES,
        },
        timeout,
    )
    if "access_token" not in payload:
        raise OAuthError(
            f"Could not refresh the Microsoft token: {_describe_failure(payload)}. "
            "Re-authorize the mailbox with `icr mailbox authorize NAME`."
        )
    return _to_pair(payload)


def _to_pair(payload: dict[str, Any]) -> TokenPair:
    return TokenPair(
        access_token=str(payload["access_token"]),
        refresh_token=str(payload["refresh_token"]) if payload.get("refresh_token") else None,
        expires_in=int(payload.get("expires_in", 3600)),
    )
