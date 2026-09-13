"""Command line interface.

The CLI talks to SQLite directly and works with `icr serve` stopped or running --
WAL mode makes concurrent access safe. Codes added here are picked up by the
running service on its next tick; there is no need to restart anything.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, NoReturn, TypeVar

import typer
from rich.console import Console
from rich.table import Table

from icr import sources
from icr.chests import buy_chests, fetch_inventory, open_chests, use_blacksmith
from icr.config import Settings, get_settings
from icr.db import DatabaseUnwritableError, ensure_writable, open_db, repo
from icr.game.api import IdleChampionsApi, build_client
from icr.game.errors import GameApiError
from icr.game.models import CHEST_LABELS, CONTRACT_LABELS, ChestType, ContractType
from icr.logging_setup import register_secret, run_context, setup_logging
from icr.mail import FoundMessage, MailError, oauth
from icr.models import MailboxAuth
from icr.redeemer import Redeemer, RunSummary
from icr.sources import email_inbox
from icr.support_url import SupportUrlError, parse_support_url

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    help="Idle Champions code redeemer.",
    no_args_is_help=True,
    add_completion=False,
)
account_app = typer.Typer(help="Manage Steam accounts.", no_args_is_help=True)
code_app = typer.Typer(help="Manage codes.", no_args_is_help=True)
chest_app = typer.Typer(help="Open and buy chests.", no_args_is_help=True)
source_app = typer.Typer(help="Inspect code sources.", no_args_is_help=True)
mailbox_app = typer.Typer(help="Manage newsletter mailboxes.", no_args_is_help=True)
db_app = typer.Typer(help="Database maintenance.", no_args_is_help=True)

app.add_typer(account_app, name="account")
app.add_typer(code_app, name="code")
app.add_typer(chest_app, name="chest")
app.add_typer(source_app, name="source")
app.add_typer(mailbox_app, name="mailbox")
app.add_typer(db_app, name="db")

T = TypeVar("T")

CHEST_CHOICES = {c.name.lower(): c for c in ChestType}
CONTRACT_CHOICES = {c.name.lower(): c for c in ContractType}


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------


@contextmanager
def session(
    *, quiet: bool = False, auto_migrate: bool = True
) -> Iterator[tuple[Settings, sqlite3.Connection]]:
    """Load settings, configure logging, open the database.

    `auto_migrate=False` is for `icr db migrate` itself, which needs to report
    what *it* applied rather than what opening the database already did.
    """
    try:
        settings = get_settings()
    except Exception as exc:
        err_console.print(f"[red]Configuration error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    setup_logging(
        level="WARNING" if quiet else settings.log_level,
        log_file=settings.log_file,
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
    )
    register_secret(*settings.secrets())

    # Checked up front rather than inside the `with`, so a bind-mount ownership
    # problem reports itself instead of surfacing as a sqlite3 error mid-command.
    try:
        ensure_writable(settings.db_path)
    except DatabaseUnwritableError as exc:
        err_console.print(f"[red]Database error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    with open_db(settings.db_path, migrate_on_open=auto_migrate) as conn:
        if auto_migrate:
            repo.load_secrets(conn)
        yield settings, conn


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


async def _with_api(
    settings: Settings, fn: Callable[[IdleChampionsApi], Coroutine[Any, Any, T]]
) -> T:
    async with build_client(settings.http_timeout_seconds) as client:
        api = IdleChampionsApi(client, request_delay=settings.request_delay_seconds)
        return await fn(api)


def fail(message: str) -> NoReturn:
    err_console.print(f"[red]{message}[/red]")
    raise typer.Exit(code=1)


def _scope_label(account_id: int | None, names: dict[int, str]) -> str:
    """Who a code is for. Public codes are the common case, so they read quietly."""
    if account_id is None:
        return "[dim]all[/dim]"
    return names.get(account_id, f"account {account_id}")


# --------------------------------------------------------------------------
# accounts
# --------------------------------------------------------------------------


@account_app.command("add")
def account_add(
    name: Annotated[str, typer.Option(help="Short label for this account, e.g. 'main'.")],
    user_id: Annotated[str | None, typer.Option(help="Game user id.")] = None,
    user_hash: Annotated[str | None, typer.Option("--hash", help="Game device hash.")] = None,
    support_url: Annotated[
        str | None,
        typer.Option(help="In-game support URL; user_id and device_hash are read from it."),
    ] = None,
) -> None:
    """Add an account, or update the credentials of an existing one."""
    if support_url:
        try:
            user_id, user_hash = parse_support_url(support_url)
        except SupportUrlError as exc:
            fail(str(exc))
    if not user_id or not user_hash:
        fail("Provide either --support-url, or both --user-id and --hash.")

    with session() as (_, conn):
        existing = repo.get_account_by_name(conn, name)
        if existing is not None:
            repo.update_credentials(conn, existing.id, user_id=user_id, user_hash=user_hash)
            console.print(f"Updated credentials for [bold]{name}[/bold].")
            return
        try:
            repo.add_account(conn, name=name, user_id=user_id, user_hash=user_hash)
        except repo.DuplicateAccountError as exc:
            fail(str(exc))
        console.print(f"Added account [bold]{name}[/bold].")


@account_app.command("list")
def account_list() -> None:
    """List configured accounts."""
    with session(quiet=True) as (_, conn):
        accounts = repo.list_accounts(conn)
        counts = repo.status_counts(conn)

    if not accounts:
        console.print("No accounts yet. Add one with [bold]icr account add[/bold].")
        return

    table = Table(title="Accounts")
    table.add_column("Name")
    table.add_column("User ID")
    table.add_column("Enabled")
    table.add_column("Credentials")
    table.add_column("Redeemed", justify="right")
    for account in accounts:
        tally = counts.get(account.name, {})
        table.add_row(
            account.name,
            account.user_id,
            "yes" if account.enabled else "[dim]no[/dim]",
            "ok" if account.credentials_ok else "[red]rejected[/red]",
            str(tally.get("success", 0)),
        )
    console.print(table)


@account_app.command("enable")
def account_enable(name: str) -> None:
    """Enable an account."""
    with session() as (_, conn):
        try:
            repo.set_account_enabled(conn, name, True)
        except repo.AccountNotFoundError as exc:
            fail(str(exc))
    console.print(f"Enabled [bold]{name}[/bold].")


@account_app.command("disable")
def account_disable(name: str) -> None:
    """Disable an account without deleting its history."""
    with session() as (_, conn):
        try:
            repo.set_account_enabled(conn, name, False)
        except repo.AccountNotFoundError as exc:
            fail(str(exc))
    console.print(f"Disabled [bold]{name}[/bold].")


@account_app.command("remove")
def account_remove(
    name: str,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Delete an account and its redemption history."""
    if not yes:
        typer.confirm(
            f"Delete account {name!r} and its entire redemption history?", abort=True
        )
    with session() as (_, conn):
        try:
            repo.remove_account(conn, name)
        except repo.AccountNotFoundError as exc:
            fail(str(exc))
    console.print(f"Removed [bold]{name}[/bold].")


@account_app.command("refresh")
def account_refresh(name: str) -> None:
    """Fetch a fresh instance id and show current inventory."""
    with session() as (settings, conn):
        try:
            account = repo.require_account(conn, name)
        except repo.AccountNotFoundError as exc:
            fail(str(exc))

        async def go(api: IdleChampionsApi):
            return await fetch_inventory(conn, api, account)

        try:
            details = run_async(_with_api(settings, go))
        except GameApiError as exc:
            fail(f"Could not refresh {name}: {exc}")

        repo.set_credentials_ok(conn, account.id, True)

    table = Table(title=f"{name} inventory")
    table.add_column("Item")
    table.add_column("Count", justify="right")
    for chest_id, count in sorted(details.chests.items()):
        try:
            label = CHEST_LABELS[ChestType(chest_id)]
        except ValueError:
            continue
        table.add_row(label, f"{count:,}")
    for contract_id, count in sorted(details.contracts.items()):
        if contract_id in {c.value for c in ContractType}:
            table.add_row(CONTRACT_LABELS[ContractType(contract_id)], f"{count:,}")
    console.print(table)


# --------------------------------------------------------------------------
# codes
# --------------------------------------------------------------------------


@code_app.command("add")
def code_add(
    codes: Annotated[list[str], typer.Argument(help="One or more codes.")],
    note: Annotated[str | None, typer.Option(help="Optional note stored with the codes.")] = None,
    account: Annotated[
        str | None,
        typer.Option(
            help="Restrict these codes to one account. Use for single-use codes "
            "such as newsletter rewards; omit for public codes."
        ),
    ] = None,
) -> None:
    """Add codes manually. Anything that looks like a code is extracted, so you
    can paste a whole message."""
    text = " ".join(codes)
    extracted = sources.extract_codes(text)
    if not extracted:
        fail(f"No codes found in {text!r}.")

    with session() as (_, conn):
        account_id = None
        if account:
            account_id = _resolve_account(conn, account).id
        added = repo.add_codes(
            conn, extracted, source="manual", note=note, account_id=account_id
        )

    for code in extracted:
        marker = "[green]new[/green]" if any(a.code == code for a in added) else "[dim]known[/dim]"
        console.print(f"  {code}  {marker}")
    scope = f" for [bold]{account}[/bold]" if account else ""
    console.print(f"{len(added)} new code(s) queued for redemption{scope}.")


@code_app.command("list")
def code_list(
    limit: Annotated[int, typer.Option(help="How many codes to show.")] = 25,
    unredeemed: Annotated[
        bool, typer.Option("--unredeemed", help="Only codes still outstanding somewhere.")
    ] = False,
) -> None:
    """List known codes."""
    with session(quiet=True) as (settings, conn):
        all_codes = repo.list_codes(conn, limit=None if unredeemed else limit)
        outstanding = {
            item.code
            for item in repo.outstanding_work(conn, max_attempts=settings.max_redeem_attempts)
        }
        account_names = {a.id: a.name for a in repo.list_accounts(conn)}

    shown = [c for c in all_codes if c.code in outstanding] if unredeemed else all_codes
    shown = shown[:limit]

    if not shown:
        console.print("No codes to show.")
        return

    table = Table(title="Codes")
    table.add_column("Code")
    table.add_column("Source")
    table.add_column("For")
    table.add_column("First seen")
    table.add_column("Outstanding")
    for code in shown:
        table.add_row(
            code.code,
            code.source,
            _scope_label(code.account_id, account_names),
            code.first_seen_at,
            "yes" if code.code in outstanding else "[dim]no[/dim]",
        )
    console.print(table)


# --------------------------------------------------------------------------
# redeem / poll
# --------------------------------------------------------------------------


@app.command()
def redeem(
    account: Annotated[str | None, typer.Option(help="Only this account.")] = None,
    code: Annotated[str | None, typer.Option(help="Only this code.")] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would be attempted, make no requests.")
    ] = False,
) -> None:
    """Redeem outstanding codes across enabled accounts."""
    with session() as (settings, conn), run_context():
        async def go(api: IdleChampionsApi) -> RunSummary:
            redeemer = Redeemer(
                conn,
                api,
                max_attempts=settings.max_redeem_attempts,
                dry_run=dry_run,
            )
            return await redeemer.run(account_name=account, code=code)

        summary = run_async(_with_api(settings, go))

    console.print(summary.describe())


@app.command()
def poll() -> None:
    """Ask every enabled source for new codes."""
    with session() as (settings, conn), run_context():
        results = run_async(sources.poll_all(conn, settings))

    if not results:
        console.print(
            "No sources are enabled. Set [bold]ICR_DISCORD_ENABLED=true[/bold] "
            "to poll Discord."
        )
        return
    for result in results:
        style = "red" if result.error else ""
        console.print(f"[{style}]{result.describe()}[/{style}]" if style else result.describe())


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------


@source_app.command("list")
def source_list() -> None:
    """Show registered code sources and where each one has read up to."""
    with session(quiet=True) as (settings, conn):
        rows = [
            (name, source.enabled(settings), source.position(conn))
            for name, source in sources.REGISTRY.items()
        ]

    if not rows:
        console.print("No sources are registered.")
        return

    table = Table(title="Code sources")
    table.add_column("Source")
    table.add_column("Enabled")
    table.add_column("Read up to")
    for name, enabled, cursor in rows:
        table.add_row(
            name,
            "yes" if enabled else "[dim]no[/dim]",
            cursor or "[dim]nothing yet[/dim]",
        )
    console.print(table)


@source_app.command("reset")
def source_reset(
    name: Annotated[str, typer.Argument(help="Source name, e.g. 'discord'.")],
) -> None:
    """Forget a source's position so the next poll re-scans recent messages.

    Codes already in the database are not re-added -- this only makes the source
    look at messages it had already passed.
    """
    source = sources.REGISTRY.get(name)
    if source is None:
        fail(f"Unknown source {name!r}. Known: {', '.join(sources.REGISTRY) or 'none'}.")

    with session() as (_, conn):
        source.reset(conn)
    console.print(f"Reset [bold]{name}[/bold]. The next `icr poll` will re-scan recent messages.")


# --------------------------------------------------------------------------
# mailboxes -- the email source
# --------------------------------------------------------------------------


@mailbox_app.command("add")
def mailbox_add(
    name: Annotated[str, typer.Option(help="Short label for this mailbox, e.g. 'main-mail'.")],
    account: Annotated[
        str,
        typer.Option(
            help="Game account this mailbox is subscribed for. Codes found here "
            "are redeemed for that account and no other."
        ),
    ],
    username: Annotated[str, typer.Option(help="IMAP login, usually the email address.")],
    host: Annotated[str | None, typer.Option(help="IMAP host, e.g. imap.gmail.com.")] = None,
    port: Annotated[int, typer.Option(help="IMAP port.")] = 993,
    microsoft: Annotated[
        bool,
        typer.Option(
            "--microsoft",
            help="Outlook/Hotmail/Office 365. Uses OAuth2; run `icr mailbox "
            "authorize` afterwards.",
        ),
    ] = False,
    client_id: Annotated[
        str | None, typer.Option(help="Azure application (client) id, if not set in the config.")
    ] = None,
    tenant: Annotated[str | None, typer.Option(help="Azure tenant. Default: common.")] = None,
    password: Annotated[
        str | None,
        typer.Option(help="IMAP password. Prompted for if omitted, which keeps it "
        "out of your shell history."),
    ] = None,
) -> None:
    """Register a mailbox to scan for newsletter codes."""
    auth = MailboxAuth.MICROSOFT if microsoft else MailboxAuth.PASSWORD

    if microsoft:
        host = host or oauth.IMAP_HOST
        if password:
            fail("Microsoft mailboxes do not use a password. Run `icr mailbox authorize` instead.")
    else:
        if not host:
            fail("--host is required (or use --microsoft for Outlook/Office 365).")
        if password is None:
            password = typer.prompt(f"IMAP password for {username}", hide_input=True)
        if not password:
            fail("A password is required for a non-Microsoft mailbox.")

    with session() as (_, conn):
        acc = _resolve_account(conn, account)
        try:
            stored = repo.add_mailbox(
                conn,
                name=name,
                account_id=acc.id,
                auth=auth,
                host=host,
                port=port,
                username=username,
                secret=password,
                oauth_client_id=client_id,
                oauth_tenant=tenant,
            )
        except repo.DuplicateMailboxError as exc:
            fail(str(exc))

    console.print(f"Added mailbox [bold]{name}[/bold] for account [bold]{account}[/bold].")
    if stored.needs_authorization:
        console.print(f"Next: [bold]icr mailbox authorize {name}[/bold]")
    else:
        console.print(f"Check it with [bold]icr mailbox test {name}[/bold]")


@mailbox_app.command("authorize")
def mailbox_authorize(
    name: Annotated[str, typer.Argument(help="Mailbox name.")],
) -> None:
    """Sign in to a Microsoft mailbox and store its refresh token.

    Prints a short code to type into microsoft.com/devicelogin on any device;
    nothing needs a browser on this machine.
    """
    with session() as (settings, conn):
        try:
            mailbox = repo.require_mailbox(conn, name)
        except repo.MailboxNotFoundError as exc:
            fail(str(exc))
        if mailbox.auth is not MailboxAuth.MICROSOFT:
            fail(f"Mailbox {name!r} uses a password, so there is nothing to authorize.")

        client_id = mailbox.oauth_client_id or settings.email_oauth_client_id
        if not client_id:
            fail(
                "No Azure client id. Set ICR_EMAIL_OAUTH_CLIENT_ID, or re-add the "
                "mailbox with --client-id."
            )
        tenant = mailbox.oauth_tenant or settings.email_oauth_tenant

        async def go() -> oauth.TokenPair:
            prompt = await oauth.begin_device_code(
                client_id=client_id, tenant=tenant, timeout=settings.http_timeout_seconds
            )
            console.print(f"\n[bold]{prompt.describe()}[/bold]\n")
            console.print("[dim]Waiting for the sign-in to complete...[/dim]")
            return await oauth.poll_device_code(
                prompt,
                client_id=client_id,
                tenant=tenant,
                timeout=settings.http_timeout_seconds,
            )

        try:
            tokens = run_async(go())
        except oauth.OAuthError as exc:
            fail(str(exc))

        if not tokens.refresh_token:
            fail(
                "Microsoft returned no refresh token. The app registration needs the "
                "'offline_access' permission."
            )
        repo.update_mailbox_secret(conn, mailbox.id, tokens.refresh_token)

    console.print(f"Authorized [bold]{name}[/bold]. Check it with `icr mailbox test {name}`.")


@mailbox_app.command("test")
def mailbox_test(
    name: Annotated[str, typer.Argument(help="Mailbox name.")],
    days: Annotated[
        int, typer.Option(help="How far back to look. 0 scans the whole mailbox.")
    ] = 30,
) -> None:
    """Connect to a mailbox and report what it finds, changing nothing.

    No codes are stored and no messages are marked as read -- this is purely a
    check that the credentials, host and sender filter are right.
    """
    from datetime import timedelta

    with session() as (settings, conn):
        try:
            mailbox = repo.require_mailbox(conn, name)
        except repo.MailboxNotFoundError as exc:
            fail(str(exc))

        since = (datetime.now(UTC).date() - timedelta(days=days)) if days else None

        # Deliberately passes no cursors: a test should report what is in the
        # mailbox over the window asked for, not what the poll has yet to read.
        # Nothing it learns is written back either.
        async def go() -> list[FoundMessage]:
            result = await email_inbox.read_mailbox(conn, settings, mailbox, since=since)
            return result.messages

        try:
            messages = run_async(go())
        except (MailError, oauth.OAuthError, sources.SourceError) as exc:
            fail(str(exc))

    found: dict[str, str] = {}
    for message in messages:
        for code in sources.extract_codes(message.text):
            found.setdefault(code, message.subject or "(no subject)")

    senders = ", ".join(settings.sender_list) or "anyone"
    window = f" in the last {days} day(s)" if days else ""
    console.print(f"{len(messages)} matching message(s) from {senders}{window}.")
    if not found:
        console.print("No codes in them.")
        return
    console.print(f"{len(found)} code(s) would go to [bold]{mailbox.account_name}[/bold]:")
    for code, subject in found.items():
        console.print(f"  {code}  [dim]{subject}[/dim]")
    console.print("\n[dim]Nothing was stored. Run `icr poll` to queue them.[/dim]")


@mailbox_app.command("list")
def mailbox_list() -> None:
    """List configured mailboxes."""
    with session(quiet=True) as (_, conn):
        mailboxes = repo.list_mailboxes(conn)

    if not mailboxes:
        console.print("No mailboxes yet. Add one with [bold]icr mailbox add[/bold].")
        return

    table = Table(title="Mailboxes")
    table.add_column("Name")
    table.add_column("Account")
    table.add_column("Address")
    table.add_column("Auth")
    table.add_column("Enabled")
    table.add_column("Last polled")
    table.add_column("Status")
    for mailbox in mailboxes:
        if mailbox.needs_authorization:
            status_cell = "[yellow]needs authorize[/yellow]"
        elif mailbox.last_error:
            status_cell = f"[red]{mailbox.last_error}[/red]"
        elif mailbox.last_polled_at:
            status_cell = "ok"
        else:
            status_cell = "[dim]never polled[/dim]"
        table.add_row(
            mailbox.name,
            mailbox.account_name or "?",
            mailbox.username,
            mailbox.auth.value,
            "yes" if mailbox.enabled else "[dim]no[/dim]",
            mailbox.last_polled_at or "[dim]never[/dim]",
            status_cell,
        )
    console.print(table)


@mailbox_app.command("enable")
def mailbox_enable(name: str) -> None:
    """Enable a mailbox."""
    _set_mailbox_enabled(name, True)


@mailbox_app.command("disable")
def mailbox_disable(name: str) -> None:
    """Stop polling a mailbox without deleting it."""
    _set_mailbox_enabled(name, False)


def _set_mailbox_enabled(name: str, enabled: bool) -> None:
    with session() as (_, conn):
        try:
            repo.set_mailbox_enabled(conn, name, enabled)
        except repo.MailboxNotFoundError as exc:
            fail(str(exc))
    console.print(f"{'Enabled' if enabled else 'Disabled'} [bold]{name}[/bold].")


@mailbox_app.command("remove")
def mailbox_remove(
    name: str,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Delete a mailbox. Codes already found through it are kept."""
    if not yes:
        typer.confirm(f"Delete mailbox {name!r} and its stored credentials?", abort=True)
    with session() as (_, conn):
        try:
            repo.remove_mailbox(conn, name)
        except repo.MailboxNotFoundError as exc:
            fail(str(exc))
    console.print(f"Removed [bold]{name}[/bold].")


# --------------------------------------------------------------------------
# status / history
# --------------------------------------------------------------------------


@app.command()
def status() -> None:
    """Show accounts, queue depth and recent activity."""
    with session(quiet=True) as (settings, conn):
        accounts = repo.list_accounts(conn)
        work = repo.outstanding_work(conn, max_attempts=settings.max_redeem_attempts)
        counts = repo.status_counts(conn)
        total_codes = len(repo.list_codes(conn))
        recent = repo.history(conn, limit=5)

    console.print(
        f"[bold]{len(accounts)}[/bold] account(s), "
        f"[bold]{total_codes}[/bold] code(s) known"
    )
    console.print(f"[bold]{len(work)}[/bold] (account, code) pair(s) outstanding")

    if accounts:
        table = Table(title="Per account")
        table.add_column("Account")
        table.add_column("Outstanding", justify="right")
        table.add_column("Redeemed", justify="right")
        table.add_column("Already had", justify="right")
        table.add_column("Failed", justify="right")
        for acc in accounts:
            tally = counts.get(acc.name, {})
            pending = sum(1 for w in work if w.account_name == acc.name)
            table.add_row(
                acc.name if acc.enabled else f"[dim]{acc.name} (disabled)[/dim]",
                str(pending),
                str(tally.get("success", 0)),
                str(tally.get("already_redeemed", 0)),
                str(tally.get("failed", 0)),
            )
        console.print(table)

    if recent:
        console.print("\n[bold]Most recent[/bold]")
        for e in recent:
            console.print(f"  {e.last_attempt_at}  {e.account_name}  {e.code}  {e.status}")


@app.command()
def history(
    account: Annotated[str | None, typer.Option(help="Only this account.")] = None,
    limit: Annotated[int, typer.Option(help="How many rows.")] = 50,
) -> None:
    """Show the redemption ledger."""
    with session(quiet=True) as (_, conn):
        entries = repo.history(conn, account_name=account, limit=limit)

    if not entries:
        console.print("Nothing redeemed yet.")
        return

    table = Table(title="Redemption history")
    table.add_column("When")
    table.add_column("Account")
    table.add_column("Code")
    table.add_column("Status")
    table.add_column("Detail")
    for entry in entries:
        detail = entry.error or ""
        if entry.loot_json and not detail:
            try:
                detail = f"{len(json.loads(entry.loot_json))} loot item(s)"
            except ValueError:
                detail = ""
        table.add_row(
            entry.last_attempt_at, entry.account_name, entry.code, entry.status.value, detail
        )
    console.print(table)


# --------------------------------------------------------------------------
# chests
# --------------------------------------------------------------------------


def _resolve_account(conn: sqlite3.Connection, name: str):
    try:
        return repo.require_account(conn, name)
    except repo.AccountNotFoundError as exc:
        fail(str(exc))


def _chest_type(value: str) -> ChestType:
    try:
        return CHEST_CHOICES[value.lower()]
    except KeyError:
        fail(f"Unknown chest type {value!r}. Choose from: {', '.join(CHEST_CHOICES)}.")


@chest_app.command("open")
def chest_open(
    account: Annotated[str, typer.Option(help="Account name.")],
    chest: Annotated[str, typer.Option(help=f"One of: {', '.join(CHEST_CHOICES)}.")],
    count: Annotated[int, typer.Option(help="How many to open.")],
) -> None:
    """Open chests."""
    chest_type = _chest_type(chest)
    with session() as (settings, conn), run_context():
        acc = _resolve_account(conn, account)

        async def go(api: IdleChampionsApi):
            return await open_chests(conn, api, acc, chest_type=chest_type, count=count)

        try:
            summary = run_async(_with_api(settings, go))
        except (GameApiError, ValueError) as exc:
            fail(str(exc))
    console.print(summary.describe())


@chest_app.command("buy")
def chest_buy(
    account: Annotated[str, typer.Option(help="Account name.")],
    chest: Annotated[str, typer.Option(help=f"One of: {', '.join(CHEST_CHOICES)}.")],
    count: Annotated[int, typer.Option(help="How many to buy.")],
    yes: Annotated[bool, typer.Option("--yes", help="Confirm the spend.")] = False,
) -> None:
    """Buy chests. Spends in-game currency."""
    chest_type = _chest_type(chest)
    if not yes:
        typer.confirm(
            f"Buy {count} {chest_type.name.lower()} chest(s) for {account}? "
            "This spends in-game currency.",
            abort=True,
        )
    with session() as (settings, conn), run_context():
        acc = _resolve_account(conn, account)

        async def go(api: IdleChampionsApi):
            return await buy_chests(conn, api, acc, chest_type=chest_type, count=count)

        try:
            bought = run_async(_with_api(settings, go))
        except (GameApiError, ValueError) as exc:
            fail(str(exc))
    console.print(f"Bought {bought} chest(s).")


@app.command()
def blacksmith(
    account: Annotated[str, typer.Option(help="Account name.")],
    hero_id: Annotated[str, typer.Option(help="Hero id to upgrade.")],
    contract: Annotated[str, typer.Option(help=f"One of: {', '.join(CONTRACT_CHOICES)}.")],
    count: Annotated[int, typer.Option(help="How many contracts to use.")],
    yes: Annotated[bool, typer.Option("--yes", help="Confirm the spend.")] = False,
) -> None:
    """Use blacksmith contracts on a hero. Spends contracts."""
    try:
        contract_type = CONTRACT_CHOICES[contract.lower()]
    except KeyError:
        fail(f"Unknown contract {contract!r}. Choose from: {', '.join(CONTRACT_CHOICES)}.")

    if not yes:
        typer.confirm(
            f"Use {count} {contract.lower()} contract(s) on hero {hero_id} for {account}?",
            abort=True,
        )

    with session() as (settings, conn), run_context():
        acc = _resolve_account(conn, account)

        async def go(api: IdleChampionsApi):
            return await use_blacksmith(
                conn, api, acc, contract=contract_type, hero_id=hero_id, count=count
            )

        try:
            summary = run_async(_with_api(settings, go))
        except (GameApiError, ValueError) as exc:
            fail(str(exc))
    console.print(summary.describe())


# --------------------------------------------------------------------------
# db / serve
# --------------------------------------------------------------------------


@db_app.command("migrate")
def db_migrate() -> None:
    """Apply pending schema migrations."""
    with session(auto_migrate=False) as (settings, conn):
        from icr.db import migrate as run_migrations

        applied = run_migrations(conn)
    if applied:
        console.print(f"Applied migration(s): {', '.join(str(v) for v in applied)}")
    else:
        console.print("Schema is up to date.")
    console.print(f"Database: {settings.db_path}")


@db_app.command("backup")
def db_backup(
    destination: Annotated[
        Path | None,
        typer.Argument(
            help="Where to write the snapshot. Defaults to a timestamped file "
            "next to the database."
        ),
    ] = None,
) -> None:
    """Write a consistent snapshot of the database.

    Safe to run while the service is going: it uses SQLite's online backup API
    rather than copying the file, so the WAL is accounted for.
    """
    from icr.db import backup as run_backup

    with session() as (settings, conn):
        target = destination or settings.db_path.with_name(
            f"{settings.db_path.stem}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.sqlite3"
        )
        try:
            written = run_backup(conn, target)
        except (DatabaseUnwritableError, sqlite3.Error) as exc:
            fail(f"Backup failed: {exc}")

    size = written.stat().st_size
    console.print(f"Backed up to [bold]{written}[/bold] ({size / 1024:.1f} KiB)")


@app.command()
def serve() -> None:
    """Run the long-lived service: scheduled polling, redeeming and the web UI."""
    from icr.service import run_service

    run_service()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
