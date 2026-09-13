"""Reading mail.

Split from `icr.sources.email_inbox` because it is plumbing, not policy: the
IMAP scan and the Microsoft OAuth2 dance know nothing about codes, accounts or
the database.
"""

from icr.mail import oauth
from icr.mail.imap import FolderCursor, FoundMessage, MailError, ScanResult, scan_mailbox
from icr.mail.oauth import (
    DeviceCodePrompt,
    OAuthError,
    TokenPair,
    begin_device_code,
    exchange_refresh_token,
    poll_device_code,
)

__all__ = [
    "DeviceCodePrompt",
    "FolderCursor",
    "FoundMessage",
    "MailError",
    "OAuthError",
    "ScanResult",
    "TokenPair",
    "begin_device_code",
    "exchange_refresh_token",
    "oauth",
    "poll_device_code",
    "scan_mailbox",
]
