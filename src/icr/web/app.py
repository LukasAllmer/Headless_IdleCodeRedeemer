"""FastAPI application: server-rendered pages, no build step, no JS framework.

The app owns the scheduler lifecycle -- it starts with the first request loop
and stops on shutdown -- so `icr serve` is a single process.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from icr import sources
from icr.chests import buy_chests, open_chests
from icr.db import repo
from icr.game.api import IdleChampionsApi, build_client
from icr.game.errors import GameApiError
from icr.game.models import CHEST_LABELS, ChestType
from icr.service import ServiceState, build_scheduler
from icr.support_url import SupportUrlError, parse_support_url

log = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
COOKIE_NAME = "icr_auth"


def create_app(state: ServiceState) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        scheduler = build_scheduler(state)
        scheduler.start()
        try:
            yield
        finally:
            scheduler.shutdown(wait=False)

    app = FastAPI(title="Idle Code Redeemer", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.icr = state

    def require_auth(request: Request) -> None:
        """No token configured means loopback-only, which `Settings` enforces."""
        token = state.settings.web_auth_token
        if not token:
            return
        cookie = request.cookies.get(COOKIE_NAME, "")
        if not secrets.compare_digest(cookie, token):
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": "/login"},
            )

    auth = Depends(require_auth)

    # ----------------------------------------------------------------------
    # auth
    # ----------------------------------------------------------------------

    @app.exception_handler(HTTPException)
    async def redirect_on_auth(request: Request, exc: HTTPException) -> Response:
        location = (exc.headers or {}).get("Location")
        if exc.status_code == status.HTTP_303_SEE_OTHER and location:
            return RedirectResponse(location, status_code=303)
        return HTMLResponse(f"<h1>{exc.status_code}</h1><p>{exc.detail}</p>", exc.status_code)

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request, bad: bool = False):
        return TEMPLATES.TemplateResponse(request, "login.html", {"bad": bad})

    @app.post("/login")
    async def login(token: str = Form(...)):
        if not secrets.compare_digest(token, state.settings.web_auth_token):
            return RedirectResponse("/login?bad=1", status_code=303)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            COOKIE_NAME, token, httponly=True, samesite="strict", max_age=30 * 86400
        )
        return response

    # ----------------------------------------------------------------------
    # dashboard
    # ----------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse, dependencies=[auth])
    async def dashboard(request: Request):
        conn = state.conn
        accounts = repo.list_accounts(conn)
        work = repo.outstanding_work(conn, max_attempts=state.settings.max_redeem_attempts)
        counts = repo.status_counts(conn)
        pending_per_account = {a.name: 0 for a in accounts}
        for item in work:
            pending_per_account[item.account_name] = (
                pending_per_account.get(item.account_name, 0) + 1
            )
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {
                "accounts": accounts,
                "counts": counts,
                "pending": pending_per_account,
                "outstanding": len(work),
                "total_codes": len(repo.list_codes(conn)),
                "recent": repo.history(conn, limit=15),
                "last_run": state.last_run,
                "last_run_at": state.last_run_at,
                "last_poll": state.last_poll,
                "sources_enabled": [
                    name
                    for name, source in sources.REGISTRY.items()
                    if source.enabled(state.settings)
                ],
            },
        )

    @app.post("/redeem", dependencies=[auth])
    async def trigger_redeem():
        await state.redeem()
        return RedirectResponse("/", status_code=303)

    @app.post("/poll", dependencies=[auth])
    async def trigger_poll():
        await state.poll_sources()
        await state.redeem()
        return RedirectResponse("/", status_code=303)

    # ----------------------------------------------------------------------
    # codes
    # ----------------------------------------------------------------------

    @app.get("/codes", response_class=HTMLResponse, dependencies=[auth])
    async def codes_page(request: Request, added: int = 0, error: str = ""):
        conn = state.conn
        accounts = repo.list_accounts(conn)
        codes = repo.list_codes(conn, limit=100)
        matrix: dict[str, dict[str, str]] = {}
        for code in codes:
            row = {}
            for account in accounts:
                if code.account_id is not None and code.account_id != account.id:
                    # A newsletter code belongs to one account; the others were
                    # never candidates, which is different from "not yet tried".
                    row[account.name] = "na"
                    continue
                entry = repo.get_redemption(conn, account_id=account.id, code_id=code.id)
                row[account.name] = entry.status.value if entry else "-"
            matrix[code.code] = row
        return TEMPLATES.TemplateResponse(
            request,
            "codes.html",
            {
                "accounts": accounts,
                "codes": codes,
                "matrix": matrix,
                "account_names": {a.id: a.name for a in accounts},
                "added": added,
                "error": error,
            },
        )

    @app.post("/codes", dependencies=[auth])
    async def add_codes(text: str = Form(...), note: str = Form(""), account: str = Form("")):
        extracted = sources.extract_codes(text)
        if not extracted:
            return RedirectResponse("/codes?error=No+codes+found+in+that+text", status_code=303)

        account_id = None
        if account.strip():
            target = repo.get_account_by_name(state.conn, account.strip())
            if target is None:
                return RedirectResponse("/codes?error=Unknown+account", status_code=303)
            account_id = target.id

        added = repo.add_codes(
            state.conn,
            extracted,
            source="manual",
            note=note.strip() or None,
            account_id=account_id,
        )
        if added:
            # Redemption has no timer, so entering a code here is one of the
            # events that starts a run. Waiting for the next poll would work but
            # makes the button feel broken.
            await state.redeem()
        return RedirectResponse(f"/codes?added={len(added)}", status_code=303)

    # ----------------------------------------------------------------------
    # accounts
    # ----------------------------------------------------------------------

    @app.get("/accounts", response_class=HTMLResponse, dependencies=[auth])
    async def accounts_page(request: Request, error: str = "", ok: str = ""):
        return TEMPLATES.TemplateResponse(
            request,
            "accounts.html",
            {
                "accounts": repo.list_accounts(state.conn),
                # Read-only: adding a mailbox means typing a password or running
                # a device-code sign-in, neither of which belongs in a web form
                # that is deliberately unauthenticated on loopback.
                "mailboxes": repo.list_mailboxes(state.conn),
                "error": error,
                "ok": ok,
            },
        )

    @app.post("/accounts", dependencies=[auth])
    async def add_account(
        name: str = Form(...),
        support_url: str = Form(""),
        user_id: str = Form(""),
        user_hash: str = Form(""),
    ):
        if support_url.strip():
            try:
                user_id, user_hash = parse_support_url(support_url)
            except SupportUrlError as exc:
                return RedirectResponse(f"/accounts?error={exc}", status_code=303)
        if not (user_id.strip() and user_hash.strip()):
            return RedirectResponse(
                "/accounts?error=Provide+a+support+URL+or+both+credentials", status_code=303
            )

        existing = repo.get_account_by_name(state.conn, name)
        if existing:
            repo.update_credentials(
                state.conn, existing.id, user_id=user_id, user_hash=user_hash
            )
            return RedirectResponse(f"/accounts?ok=Updated+{name}", status_code=303)
        try:
            repo.add_account(
                state.conn, name=name, user_id=user_id, user_hash=user_hash
            )
        except repo.DuplicateAccountError as exc:
            return RedirectResponse(f"/accounts?error={exc}", status_code=303)
        return RedirectResponse(f"/accounts?ok=Added+{name}", status_code=303)

    @app.post("/accounts/{name}/toggle", dependencies=[auth])
    async def toggle_account(name: str):
        account = repo.get_account_by_name(state.conn, name)
        if account:
            repo.set_account_enabled(state.conn, name, not account.enabled)
        return RedirectResponse("/accounts", status_code=303)

    # ----------------------------------------------------------------------
    # chests
    # ----------------------------------------------------------------------

    @app.get("/chests", response_class=HTMLResponse, dependencies=[auth])
    async def chests_page(request: Request, result: str = "", error: str = ""):
        return TEMPLATES.TemplateResponse(
            request,
            "chests.html",
            {
                "accounts": repo.list_accounts(state.conn, enabled_only=True),
                "chest_types": [(c.name.lower(), CHEST_LABELS[c]) for c in ChestType],
                "result": result,
                "error": error,
            },
        )

    @app.post("/chests", dependencies=[auth])
    async def chest_action(
        account: str = Form(...),
        chest: str = Form(...),
        count: int = Form(...),
        action: str = Form(...),
    ):
        acc = repo.get_account_by_name(state.conn, account)
        if acc is None:
            return RedirectResponse("/chests?error=Unknown+account", status_code=303)
        try:
            chest_type = ChestType[chest.upper()]
        except KeyError:
            return RedirectResponse("/chests?error=Unknown+chest+type", status_code=303)

        try:
            async with state.lock, build_client(state.settings.http_timeout_seconds) as client:
                api = IdleChampionsApi(
                    client, request_delay=state.settings.request_delay_seconds
                )
                if action == "open":
                    summary = await open_chests(
                        state.conn, api, acc, chest_type=chest_type, count=count
                    )
                    message = summary.describe()
                elif action == "buy":
                    bought = await buy_chests(
                        state.conn, api, acc, chest_type=chest_type, count=count
                    )
                    message = f"Bought {bought} chest(s)."
                else:
                    return RedirectResponse("/chests?error=Unknown+action", status_code=303)
        except (GameApiError, ValueError) as exc:
            return RedirectResponse(f"/chests?error={exc}", status_code=303)

        return RedirectResponse(f"/chests?result={message}", status_code=303)

    return app
