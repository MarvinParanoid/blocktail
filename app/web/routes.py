"""HTTP layer: server-rendered HTML fragments for HTMX, plus a small JSON API.

The JSON contracts use chain-neutral names (``chain``, ``account``, ``asset``,
``activity``, ``counterparty``) rather than Ethereum-specific ones.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.chains import UnknownChainError, get_chain, supported_chains
from decimal import Decimal

from app.chains.ethereum.known_assets import KNOWN_TOKENS, looks_forged

from app.models import NATIVE, Direction, Scope
from app.web.assets import static_url
from app.web.format import (
    Relevance,
    build_activity,
    build_portfolio,
    build_status,
    build_transaction,
    group_by_transaction,
    build_summary,
    build_wallets,
    format_amount,
)

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["static_url"] = static_url

PAGE_SIZE = 50

router = APIRouter()


@dataclass(slots=True)
class Filters:
    account_id: int | None = None
    scope: Scope = Scope.ALL
    show_dust: bool = False
    direction: Direction = Direction.ALL
    asset_id: int | None = None
    search: str = ""
    cursor: tuple[int, int] | None = None

    @property
    def query_string(self) -> str:
        parts = []
        if self.account_id is not None:
            parts.append(f"wallet={self.account_id}")
        elif self.scope is not Scope.ALL:
            parts.append(f"scope={self.scope.value}")
        if self.direction is not Direction.ALL:
            parts.append(f"direction={self.direction.value}")
        if self.asset_id is not None:
            parts.append(f"asset={self.asset_id}")
        if self.search:
            from urllib.parse import quote_plus

            parts.append(f"q={quote_plus(self.search)}")
        if self.show_dust:
            parts.append("dust=show")
        return "&".join(parts)

    @property
    def is_active(self) -> bool:
        return (
            self.account_id is not None
            or self.scope is not Scope.ALL
            or self.direction is not Direction.ALL
            or self.asset_id is not None
            or bool(self.search)
        )


def _checked(value: str | None) -> bool:
    return (value or "").lower() in {"on", "true", "1", "yes"}


def _same_origin(request: Request) -> bool:
    """Reject a state-changing request that another site made on the user's
    behalf. `Sec-Fetch-Site` is sent by every current browser; the Origin header
    is the fallback for anything that is not."""
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site in {"same-origin", "same-site", "none"}
    origin = request.headers.get("origin")
    if origin is None:
        return True  # curl and friends: nothing to forge a session with anyway
    host = request.headers.get("host", "")
    return origin.split("//")[-1] == host


def _parse_int(raw: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _parse_filters(
    wallet: str | None,
    direction: str | None,
    asset: str | None,
    q: str | None,
    before: str | None,
    scope: str | None = None,
    dust: str | None = None,
) -> Filters:
    try:
        parsed_direction = Direction((direction or "all").lower())
    except ValueError:
        parsed_direction = Direction.ALL

    try:
        parsed_scope = Scope((scope or "all").lower())
    except ValueError:
        parsed_scope = Scope.ALL

    cursor: tuple[int, int] | None = None
    if before and "_" in before:
        head, _, tail = before.partition("_")
        timestamp, activity_id = _parse_int(head), _parse_int(tail)
        if timestamp is not None and activity_id is not None:
            cursor = (timestamp, activity_id)

    return Filters(
        account_id=_parse_int(wallet),
        scope=parsed_scope,
        show_dust=(dust or "").lower() == "show",
        direction=parsed_direction,
        asset_id=_parse_int(asset),
        search=(q or "").strip()[:120],
        cursor=cursor,
    )


def _scope_ids(ctx, filters: Filters) -> set[int] | None:
    """The accounts the feed is looking through. ``None`` means all of them."""
    if filters.account_id is not None:
        return {filters.account_id}
    if filters.scope is Scope.OWNED:
        return {a.id for a in ctx.db.list_accounts() if a.is_owned}
    if filters.scope is Scope.WATCHED:
        return {a.id for a in ctx.db.list_accounts() if not a.is_owned}
    return None


SCOPE_LABELS = {Scope.ALL: "All activity", Scope.OWNED: "My wallets", Scope.WATCHED: "Watching"}


def _scope_label(ctx, filters: Filters) -> str:
    """What the feed is currently showing, for the narrow-screen selector."""
    if filters.account_id is not None:
        account = next((a for a in ctx.db.list_accounts() if a.id == filters.account_id), None)
        if account is not None:
            return account.name
    return SCOPE_LABELS[filters.scope]


def _dust_thresholds(ctx) -> dict[int, float]:
    """The raw-amount ceiling below which a transfer of each asset is dust.

    Derived from the price, so an asset with no price never qualifies: judging a
    transfer by its raw amount would make 0.000059 USDT and 0.000059 WBTC the
    same thing.
    """
    ceiling = ctx.settings.dust_transfer_usd
    if ceiling <= 0:
        return {}

    thresholds: dict[int, float] = {}
    decimals = {row["id"]: row["decimals"] for row in ctx.db.feed_assets()}
    for asset_id, row in ctx.db.prices_by_asset().items():
        price = Decimal(row["price"])
        if price <= 0 or asset_id not in decimals:
            continue
        thresholds[asset_id] = float((ceiling / price) * (Decimal(10) ** decimals[asset_id]))
    return thresholds


def _counts(ctx, filters: Filters, scope_ids) -> dict:
    """How many the feed is showing, and how many dust transfers it is not."""
    common = dict(
        account_ids=None if scope_ids is None else sorted(scope_ids),
        direction=filters.direction,
        asset_id=filters.asset_id,
        search=filters.search,
    )
    everything = ctx.db.count_activities(**common)
    if filters.show_dust:
        return {"total": everything, "dust_hidden": 0, "unverified_hidden": 0}

    unrecognised = _unrecognised_asset_ids(ctx)
    shown = ctx.db.count_activities(
        **common, dust_thresholds=_dust_thresholds(ctx), exclude_assets=unrecognised
    )
    # Counted apart because they are different accusations. A transfer of 0.87
    # in ETH is neither: it is small, and perfectly ordinary.
    without_unverified = ctx.db.count_activities(**common, exclude_assets=unrecognised)
    return {
        "total": shown,
        "dust_hidden": without_unverified - shown,
        "unverified_hidden": everything - without_unverified,
    }


def _load_page(ctx, filters: Filters) -> tuple[list, str | None]:
    scope = _scope_ids(ctx, filters)
    rows = ctx.db.query_activities(
        account_ids=None if scope is None else sorted(scope),
        direction=filters.direction,
        asset_id=filters.asset_id,
        search=filters.search,
        dust_thresholds=None if filters.show_dust else _dust_thresholds(ctx),
        exclude_assets=None if filters.show_dust else _unrecognised_asset_ids(ctx),
        cursor=filters.cursor,
        limit=PAGE_SIZE,
    )
    has_more = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    prices = ctx.db.prices_by_asset()
    activities = [build_activity(row, ctx.chain, scope, prices) for row in rows]
    # The cursor comes from the query's order, not the display order: grouping
    # moves rows within a transaction, and paging from a moved row would skip
    # or repeat whatever sat between.
    next_cursor = None
    if has_more and activities:
        timestamp, activity_id = activities[-1].cursor
        next_cursor = f"{timestamp}_{activity_id}"
    return activities, next_cursor


def _status_view(ctx):
    return build_status(
        ctx.db.get_sync_status(ctx.chain.chain_id),
        ctx.chain,
        stale_after=ctx.settings.stale_after,
    )


def _wallet_views(ctx, selected_id: int | None = None):
    return build_wallets(
        ctx.db.list_accounts(),
        ctx.db.balances_by_account(),
        ctx.chain,
        selected_id=selected_id,
        prices=ctx.db.prices_by_asset(),
        relevance=_relevance(ctx),
        max_share=ctx.settings.value_max_share,
    )


def _relevance(ctx) -> Relevance:
    """Assembled per request: it depends on prices, on config and on what the
    wallets have actually done."""
    trusted = frozenset(address for _, address in ctx.config.trusted_assets)
    return Relevance(
        known=frozenset(KNOWN_TOKENS),
        trusted=trusted,
        sent_asset_ids=frozenset(ctx.db.sent_asset_ids()),
        impostor_asset_ids=frozenset(
            row["id"]
            for row in ctx.db.all_assets()
            if row["contract_address"] not in trusted
            and looks_forged(row["symbol"], row["contract_address"])
        ),
        dust_below=ctx.settings.dust_below_usd,
    )


def _unrecognised_asset_ids(ctx) -> set[int]:
    """Assets with no market price and nothing vouching for them.

    A transfer of one of these is what the noise on a live address is made of:
    an airdrop of something with no price, no listing and no history with you.
    Deliberately not counting "you sent it" — that signal is forgeable, and the
    tokens it would rescue are exactly the ones forging it.
    """
    rules = _relevance(ctx)
    priced = set(ctx.db.prices_by_asset())
    return {
        row["id"]
        for row in ctx.db.all_assets()
        if row["contract_address"] != NATIVE
        and row["id"] not in priced
        and row["contract_address"] not in rules.known
        and row["contract_address"] not in rules.trusted
    } | set(rules.impostor_asset_ids)


def _feed_assets(ctx) -> tuple[list, list]:
    """Assets for the filter dropdown, split into the ones worth listing and the
    dust. Without balances there is no value to judge by, so the test is the
    part that does not need one: known, vouched for, or sent by a wallet."""
    rules = _relevance(ctx)
    shown, hidden = [], []
    for row in ctx.db.feed_assets():
        target = shown if rules.is_relevant(row["id"], row["contract_address"], None) else hidden
        target.append(row)
    return shown, hidden


def _portfolio(ctx, selected_id: int | None = None, *, watched_scope: bool = False):
    return build_portfolio(
        ctx.db.list_accounts(),
        ctx.db.balances_by_account(),
        ctx.db.prices_by_asset(),
        ctx.chain,
        selected_id=selected_id,
        watched_scope=watched_scope,
        max_assets=ctx.settings.max_token_balances,
        max_share=ctx.settings.value_max_share,
        relevance=_relevance(ctx),
        stale_after=max(3600, ctx.settings.price_refresh * 6),
    )


def _summary(ctx):
    return build_summary(ctx.db.list_accounts(), ctx.db.balances_by_account(), ctx.chain)


# ------------------------------------------------------------------- pages


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    wallet: str | None = Query(None),
    direction: str | None = Query(None),
    asset: str | None = Query(None),
    q: str | None = Query(None),
    scope: str | None = Query(None),
    dust: str | None = Query(None),
):
    ctx = request.app.state.ctx
    filters = _parse_filters(wallet, direction, asset, q, None, scope, dust)
    activities, next_cursor = _load_page(ctx, filters)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            **_sidebar_context(ctx, wallet, scope),
            "summary": _summary(ctx),
            "scope_label": _scope_label(ctx, filters),
            "scope_is_panel": filters.account_id is not None,
            "portfolio": _portfolio(
                ctx, filters.account_id, watched_scope=filters.scope is Scope.WATCHED
            ),
            "prices_on": ctx.price_source is not None,
            "assets": _feed_assets(ctx),
            "activities": activities,
            "groups": group_by_transaction(activities),
            "next_cursor": next_cursor,
            "previous_day": None,
            "filters": filters,
            "status": _status_view(ctx),
            "history_floor": ctx.db.history_floor(ctx.chain.chain_id),
            "older_days": ctx.settings.backfill_days,
            **_counts(ctx, filters, _scope_ids(ctx, filters)),
        },
    )


@router.get("/lab", response_class=HTMLResponse)
def lab(
    request: Request,
    wallet: str | None = Query(None),
    direction: str | None = Query(None),
    asset: str | None = Query(None),
    q: str | None = Query(None),
    scope: str | None = Query(None),
):
    """An alternative composition, kept alongside the current one to compare.

    No standing sidebar, so the activity area is a rectangle rather than an L;
    scope as a row of tabs; an unlabelled toolbar; the table inside a bounded
    pane; and a larger type scale with more separation between the three levels
    of information in a row.
    """
    ctx = request.app.state.ctx
    filters = _parse_filters(wallet, direction, asset, q, None, scope)
    activities, next_cursor = _load_page(ctx, filters)
    scope_ids = _scope_ids(ctx, filters)

    return templates.TemplateResponse(
        request,
        "index_lab.html",
        {
            **_sidebar_context(ctx, wallet, scope),
            "summary": _summary(ctx),
            "scope_label": _scope_label(ctx, filters),
            "scope_is_panel": filters.account_id is not None,
            "portfolio": _portfolio(
                ctx, filters.account_id, watched_scope=filters.scope is Scope.WATCHED
            ),
            "prices_on": ctx.price_source is not None,
            "assets": _feed_assets(ctx),
            "activities": activities,
            "groups": group_by_transaction(activities),
            "next_cursor": next_cursor,
            "previous_day": None,
            "filters": filters,
            "status": _status_view(ctx),
            "history_floor": ctx.db.history_floor(ctx.chain.chain_id),
            "older_days": ctx.settings.backfill_days,
            **_counts(ctx, filters, scope_ids),
            "stylesheet": "lab.css",
        },
    )


@router.get("/lab/tabs", response_class=HTMLResponse)
def lab_tabs(
    request: Request,
    wallet: str | None = Query(None),
    scope: str | None = Query(None),
):
    ctx = request.app.state.ctx
    return templates.TemplateResponse(
        request, "_scope_tabs.html", _sidebar_context(ctx, wallet, scope)
    )


@router.get("/activity", response_class=HTMLResponse)
def activity_fragment(
    request: Request,
    wallet: str | None = Query(None),
    direction: str | None = Query(None),
    asset: str | None = Query(None),
    q: str | None = Query(None),
    before: str | None = Query(None),
    after_day: str | None = Query(None),
    scope: str | None = Query(None),
    dust: str | None = Query(None),
):
    ctx = request.app.state.ctx
    filters = _parse_filters(wallet, direction, asset, q, before, scope, dust)
    activities, next_cursor = _load_page(ctx, filters)

    return templates.TemplateResponse(
        request,
        "_rows.html",
        {
            "activities": activities,
            "groups": group_by_transaction(activities),
            "next_cursor": next_cursor,
            # The day heading must not repeat when a page continues the day the
            # previous page ended on, so the sentinel passes that day back.
            "previous_day": (after_day or "")[:10] or None,
            "filters": filters,
            "is_page": filters.cursor is not None,
            "history_floor": ctx.db.history_floor(ctx.chain.chain_id),
            "older_days": ctx.settings.backfill_days,
        },
    )


def _sidebar_context(ctx, wallet: str | None, scope: str | None, **extra) -> dict:
    selected = _parse_int(wallet)
    wallets = _wallet_views(ctx, selected)
    try:
        parsed_scope = Scope((scope or "all").lower())
    except ValueError:
        parsed_scope = Scope.ALL
    return {
        "wallets": wallets,
        "owned": [w for w in wallets if w.is_owned],
        "watched": [w for w in wallets if not w.is_owned],
        "selected_id": selected,
        "scope": parsed_scope.value,
        "chain": ctx.chain,
        **extra,
    }


async def _transaction_cost(ctx, tx_hash: str):
    """The fee, if the provider will say. A failure here is not an error page.

    This is the one call in the app made because a person asked for it rather
    than on a timer, and it is decoration on a panel that is already useful —
    so a provider that is down, rate-limited or simply does not keep receipts
    costs the reader a line, not the transaction they came to look at.
    """
    getter = getattr(ctx.provider, "get_transaction_cost", None)
    if getter is None:
        return None
    try:
        return await getter(tx_hash)
    except Exception as exc:
        log.info("transaction cost unavailable for %s: %s", tx_hash, exc)
        return None


def _native_price(ctx, prices) -> Decimal | None:
    """What the chain's own currency is worth, for pricing a fee."""
    asset_id = ctx.db.native_asset_id(ctx.chain.chain_id)
    quote = prices.get(asset_id) if asset_id is not None else None
    return None if quote is None else Decimal(quote["price"])


@router.get("/tx/{activity_id}", response_class=HTMLResponse)
async def transaction_inspector(request: Request, activity_id: int):
    """The whole transaction behind one row of the feed.

    Clicking a row used to leave for Etherscan, which is a strange thing for a
    monitor to do with its own index. What the feed cannot show without turning
    into a tree — the other transfers this one transaction caused — is here.
    """
    ctx = request.app.state.ctx
    row = ctx.db.get_activity(activity_id)
    if row is None:
        return HTMLResponse("<p class='sheet__error'>That activity is gone.</p>", status_code=404)

    rows = ctx.db.transfers_in_tx(row["chain_id"], row["tx_hash"])
    status = ctx.db.get_sync_status(ctx.chain.chain_id)
    prices = ctx.db.prices_by_asset()
    return templates.TemplateResponse(
        request,
        "_inspector.html",
        {
            "tx": build_transaction(
                rows or [row],
                ctx.chain,
                selected_id=activity_id,
                prices=prices,
                head_block=status.head_block,
                cost=await _transaction_cost(ctx, row["tx_hash"]),
                native_price=_native_price(ctx, prices),
            )
        },
    )


@router.post("/history/older", response_class=HTMLResponse)
async def load_older_history(
    request: Request,
    wallet: str | None = Form(None),
    direction: str | None = Form(None),
    asset: str | None = Form(None),
    q: str | None = Form(None),
    scope: str | None = Form(None),
    dust: str | None = Form(None),
    before: str | None = Form(None),
    after_day: str | None = Form(None),
):
    """Reach further back on demand, then hand over the page it uncovered.

    The feed ending is not the same as the history ending — it ends where the
    backfill window was set. This moves that window and fetches, so the button
    behaves like "load more" and simply takes longer the first time.
    """
    ctx = request.app.state.ctx
    if not _same_origin(request):
        return HTMLResponse("", status_code=403)

    floor = ctx.db.history_floor(ctx.chain.chain_id)
    status = ctx.db.get_sync_status(ctx.chain.chain_id)
    reference = min(x for x in (floor, status.requested_from_block) if x is not None) if (
        floor is not None or status.requested_from_block is not None
    ) else (status.head_block or 0)
    target = max(0, reference - ctx.settings.backfill_blocks)
    ctx.db.request_history_from(ctx.chain.chain_id, target)
    log.info("history extended to block %s on request", target)

    await ctx.indexer.run_once()

    filters = _parse_filters(wallet, direction, asset, q, before, scope, dust)
    activities, next_cursor = _load_page(ctx, filters)
    return templates.TemplateResponse(
        request,
        "_rows.html",
        {
            "activities": activities,
            "groups": group_by_transaction(activities),
            "next_cursor": next_cursor,
            "previous_day": (after_day or "")[:10] or None,
            "filters": filters,
            "is_page": True,
            "history_floor": ctx.db.history_floor(ctx.chain.chain_id),
            "older_days": ctx.settings.backfill_days,
        },
    )


@router.get("/dust-note", response_class=HTMLResponse)
def dust_note(
    request: Request,
    wallet: str | None = Query(None),
    direction: str | None = Query(None),
    asset: str | None = Query(None),
    q: str | None = Query(None),
    scope: str | None = Query(None),
    dust: str | None = Query(None),
):
    """The line above the feed saying what it is not showing."""
    ctx = request.app.state.ctx
    filters = _parse_filters(wallet, direction, asset, q, None, scope, dust)
    return templates.TemplateResponse(
        request,
        "_dust_note.html",
        {"filters": filters, **_counts(ctx, filters, _scope_ids(ctx, filters))},
    )


@router.get("/wallets", response_class=HTMLResponse)
def wallets_fragment(
    request: Request,
    wallet: str | None = Query(None),
    scope: str | None = Query(None),
):
    ctx = request.app.state.ctx
    return templates.TemplateResponse(
        request, "_wallets.html", _sidebar_context(ctx, wallet, scope)
    )


def _form_response(request, ctx, *, account=None, form=None, error=None, status=200):
    return templates.TemplateResponse(
        request,
        "_wallet_form.html",
        {
            "account": account,
            "form": form or {},
            "error": error,
            "chains": supported_chains(),
            "chain": ctx.chain,
        },
        status_code=status,
        headers=None if error else {"HX-Trigger": "wallet-changed"},
    )


@router.get("/wallets/form", response_class=HTMLResponse)
def wallet_form(request: Request, id: str | None = Query(None)):
    """The add/edit form, rendered server-side so the dialog holds no state."""
    ctx = request.app.state.ctx
    account_id = _parse_int(id)
    account = next(
        (a for a in ctx.db.list_accounts() if a.id == account_id), None
    ) if account_id else None
    return templates.TemplateResponse(
        request,
        "_wallet_form.html",
        {
            "account": account,
            "form": {},
            "error": None,
            "chains": supported_chains(),
            "chain": ctx.chain,
        },
    )


@router.post("/wallets", response_class=HTMLResponse)
def add_wallet(
    request: Request,
    name: str = Form(""),
    address: str = Form(""),
    chain: str = Form("ethereum"),
    owned: str = Form(""),
):
    """Add an address to watch.

    The app has no login by design, so this is protected the way the rest of it
    is: by whatever fronts the deployment. What it does check is that the request
    came from this page rather than being cross-posted from somewhere else.
    """
    ctx = request.app.state.ctx
    if not _same_origin(request):
        return _form_response(
            request, ctx, error="Rejected a cross-site request.", status=403
        )

    name = (name or "").strip()[:60]
    is_owned = _checked(owned)
    form = {"name": name, "address": (address or "").strip(), "owned": is_owned, "chain": chain}

    if not name:
        return _form_response(request, ctx, form=form, error="Give the wallet a name.", status=422)
    try:
        normalized = get_chain(chain).normalize_address(address or "")
    except (UnknownChainError, ValueError) as exc:
        return _form_response(request, ctx, form=form, error=str(exc), status=422)

    try:
        added = ctx.db.add_account(
            chain_id=chain, address=normalized, name=name, is_owned=is_owned
        )
    except ValueError as exc:
        return _form_response(request, ctx, form=form, error=str(exc), status=422)

    # Attribute any history already indexed through another wallet; the next
    # cycle reaches back for the rest.
    ctx.db.relink_accounts()
    log.info("added account %s (%s) via the UI", added.name, added.address)
    return _form_response(request, ctx)


@router.post("/wallets/{account_id}", response_class=HTMLResponse)
def edit_wallet(
    request: Request,
    account_id: int,
    name: str = Form(None),
    # Explicitly "true"/"false" rather than a checkbox: an absent field and an
    # unticked one are indistinguishable in a form post, and here they mean
    # different things — "leave ownership alone" versus "make it watched".
    set_owned: str = Form(None),
):
    """Rename an account, or move it between "mine" and "watching"."""
    ctx = request.app.state.ctx
    if not _same_origin(request):
        return _form_response(request, ctx, error="Rejected a cross-site request.", status=403)

    account = next((a for a in ctx.db.list_accounts() if a.id == account_id), None)
    if account is None:
        return _form_response(request, ctx, error="No such wallet.", status=404)

    if name is not None:
        cleaned = name.strip()[:60]
        if not cleaned:
            return _form_response(
                request, ctx, account=account, form={"name": name},
                error="Give the wallet a name.", status=422,
            )
        if ctx.db.rename_account(account_id, cleaned) is None:
            return _form_response(
                request, ctx, account=account, form={"name": cleaned},
                error="That wallet is declared in the config file; rename it there.",
                status=422,
            )

    if set_owned is not None:
        if ctx.db.set_account_ownership(account_id, set_owned == "true") is None:
            return _form_response(
                request, ctx, account=account,
                error="That wallet is declared in the config file; change it there.",
                status=422,
            )

    log.info("updated account %s", account.name)
    return _form_response(request, ctx)


@router.post("/wallets/{account_id}/remove", response_class=HTMLResponse)
def remove_wallet(request: Request, account_id: int):
    """Stop watching an account that was added here.

    Without this the add form is a trap: a mistyped address could never be taken
    back. The row is archived rather than deleted, so re-adding the address later
    resumes indexing instead of replaying its whole history.
    """
    ctx = request.app.state.ctx
    if not _same_origin(request):
        return _form_response(request, ctx, error="Rejected a cross-site request.", status=403)

    removed = ctx.db.remove_account(account_id)
    if not removed:
        return _form_response(
            request, ctx,
            error="That wallet is declared in the config file; remove it there.",
            status=422,
        )
    log.info("stopped watching account %s", removed)
    return _form_response(request, ctx)


@router.get("/summary", response_class=HTMLResponse)
def summary_fragment(
    request: Request,
    wallet: str | None = Query(None),
    scope: str | None = Query(None),
):
    """Holdings for the current scope: my wallets, or the selected one.

    The feed's scope is a separate thing — the panel always totals what is mine —
    so the narrow-screen selector gets its own label.
    """
    ctx = request.app.state.ctx
    filters = _parse_filters(wallet, None, None, None, None, scope)
    return templates.TemplateResponse(
        request,
        "_summary.html",
        {
            "portfolio": _portfolio(
                ctx, filters.account_id, watched_scope=filters.scope is Scope.WATCHED
            ),
            "scope_label": _scope_label(ctx, filters),
            "scope_is_panel": filters.account_id is not None,
            "prices_on": ctx.price_source is not None,
        },
    )


@router.get("/holdings", response_class=HTMLResponse)
def holdings_sheet(
    request: Request,
    wallet: str | None = None,
    scope: str | None = None,
):
    """Everything the scope holds, for a screen too narrow to list it in place.

    The panel above shows a fixed three so that its height does not depend on
    how many tokens a wallet has been sent; this is where the rest live.
    """
    ctx = request.app.state.ctx
    filters = _parse_filters(wallet, None, None, None, None, scope)
    return templates.TemplateResponse(
        request,
        "_holdings.html",
        {
            "portfolio": _portfolio(
                ctx, filters.account_id, watched_scope=filters.scope is Scope.WATCHED
            )
        },
    )


@router.get("/status", response_class=HTMLResponse)
def status_fragment(
    request: Request,
    wallet: str | None = None,
    scope: str | None = None,
):
    """The whole top bar beside the brand: the scope, the account summary and
    the sync indicator. They poll together because they sit together."""
    ctx = request.app.state.ctx
    filters = _parse_filters(wallet, None, None, None, None, scope)
    return templates.TemplateResponse(
        request,
        "_status.html",
        {
            "status": _status_view(ctx),
            "summary": _summary(ctx),
            "scope_label": _scope_label(ctx, filters),
        },
    )


# --------------------------------------------------------------- json api


def _activity_json(view) -> dict:
    return {
        "id": view.id,
        "chain": view.chain_id,
        "kind": view.kind,
        "direction": view.direction,
        "timestamp": view.timestamp,
        "block_number": view.block_number,
        "tx_hash": view.tx_hash,
        "account": view.wallet,
        "asset": {"symbol": view.symbol},
        "amount": view.amount_text,
        "value": view.value_text,
        "from": {
            "address": view.source.address,
            "label": view.source.display if view.source.is_labelled else None,
            "monitored": view.source.is_monitored,
        },
        "to": {
            "address": view.target.address,
            "label": view.target.display if view.target.is_labelled else None,
            "monitored": view.target.is_monitored,
        },
    }


@router.get("/api/activity")
def api_activity(
    request: Request,
    wallet: str | None = Query(None),
    direction: str | None = Query(None),
    asset: str | None = Query(None),
    q: str | None = Query(None),
    before: str | None = Query(None),
    scope: str | None = Query(None),
    dust: str | None = Query(None),
):
    ctx = request.app.state.ctx
    filters = _parse_filters(wallet, direction, asset, q, before, scope, dust)
    activities, next_cursor = _load_page(ctx, filters)
    return {
        "activities": [_activity_json(view) for view in activities],
        "next_cursor": next_cursor,
    }


@router.get("/api/accounts")
def api_accounts(request: Request):
    ctx = request.app.state.ctx
    balances = ctx.db.balances_by_account()
    return {
        "accounts": [
            {
                "id": account.id,
                "chain": account.chain_id,
                "name": account.name,
                "owned": account.is_owned,
                "source": account.source,
                "address": ctx.chain.display_address(account.address),
                "balances": [
                    {
                        "symbol": row["symbol"],
                        "contract": row["contract_address"] or None,
                        "decimals": row["decimals"],
                        "amount": format_amount(row["amount_raw"], row["decimals"]),
                        "amount_raw": row["amount_raw"],
                        "native": row["contract_address"] == NATIVE,
                    }
                    for row in balances.get(account.id, [])
                ],
            }
            for account in ctx.db.list_accounts()
        ]
    }


@router.get("/api/portfolio")
def api_portfolio(
    request: Request,
    wallet: str | None = Query(None),
    scope: str | None = Query(None),
):
    ctx = request.app.state.ctx
    filters = _parse_filters(wallet, None, None, None, None, scope)
    view = _portfolio(ctx, filters.account_id, watched_scope=filters.scope is Scope.WATCHED)
    return {
        "scope": view.scope,
        "scope_meta": view.scope_meta or view.scope_count,
        "accounts": view.account_count,
        "total": view.total_text,
        "priced": view.priced,
        "unpriced_assets": view.unpriced,
        "hidden_assets": view.hidden,
        "excluded_assets": view.excluded,
        "prices_updated": view.price_age_text,
        "prices_stale": view.prices_stale,
        "assets": [
            {
                "symbol": asset.symbol,
                "amount": asset.amount_text,
                "value": asset.value_text,
                "native": asset.is_native,
            }
            for asset in view.assets
        ],
    }


@router.get("/api/status")
def api_status(request: Request):
    ctx = request.app.state.ctx
    status = ctx.db.get_sync_status(ctx.chain.chain_id)
    view = _status_view(ctx)
    return {
        "chain": status.chain_id,
        "state": view.state,
        "head_block": status.head_block,
        "last_synced_block": status.last_synced_block,
        "last_success_at": status.last_success_at,
        "last_error": status.last_error,
        "activity_count": status.activity_count,
        "backfill_done": status.backfill_done,
    }


@router.get("/healthz")
def healthz(request: Request):
    """Liveness for the container healthcheck.

    Deliberately stays 200 while the provider is down: a provider outage should
    not make the orchestrator restart a container that is serving fine. The sync
    state is in the body (and loudly in the UI) instead.
    """
    ctx = request.app.state.ctx
    try:
        status = ctx.db.get_sync_status(ctx.chain.chain_id)
    except Exception as exc:  # database unreachable -> genuinely unhealthy
        log.exception("health check failed")
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=503)

    # This path is exempt from authentication so the container healthcheck can
    # reach it, so with auth on it says nothing about the instance beyond being
    # alive.
    if ctx.settings.auth_enabled:
        return {"status": "ok"}
    return {
        "status": "ok",
        "chain": status.chain_id,
        "sync_state": _status_view(ctx).state,
        "head_block": status.head_block,
        "activities": status.activity_count,
    }
