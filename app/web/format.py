"""View models: turn database rows into something a template can render without
knowing anything about chains, decimals or transfer semantics."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext

from app.chains import Chain
from app.models import NATIVE, SyncStatus, TransactionCost


def format_amount(amount_raw: int | str, decimals: int | None, *, max_dp: int = 6) -> str:
    """Exact decimal rendering of a raw integer amount. Never uses a float.

    Token supplies can run to tens of digits, which overflows decimal's default
    28-digit context, so the arithmetic runs in a context sized to the input.

    ``decimals=None`` means the chain would not say: the raw integer is shown
    as it is, and the view marks it as raw units rather than whole tokens.
    """
    raw = int(amount_raw)
    if decimals is None:
        return f"{raw:,}"
    if raw == 0:
        return "0"

    with localcontext() as context:
        context.prec = len(str(abs(raw))) + decimals + max_dp + 8
        value = Decimal(raw).scaleb(-decimals)

        magnitude = abs(value)
        if magnitude >= 1000:
            places = 2
        elif magnitude >= 1:
            places = 4
        else:
            places = max_dp

        quantized = value.quantize(Decimal(1).scaleb(-places))
        # copy_abs(), not abs(): abs() is a context operation and would round
        # the value back down to the default precision.
        magnitude_text = f"{quantized.copy_abs():f}"

    if quantized == 0:
        return f"<0.{'0' * (places - 1)}1"

    sign = "-" if quantized < 0 else ""
    whole, _, fraction = magnitude_text.partition(".")
    fraction = fraction.rstrip("0")
    grouped = f"{int(whole):,}"
    return f"{sign}{grouped}.{fraction}" if fraction else f"{sign}{grouped}"


def format_money(value: Decimal | None, *, currency_symbol: str = "$") -> str | None:
    """Money is rounded for reading, not for accounting: whole dollars once past
    a thousand, cents below it, and a floor marker instead of a bare zero."""
    if value is None:
        return None
    with localcontext() as context:
        context.prec = 40
        magnitude = value.copy_abs()
        if magnitude >= 1000:
            rounded = value.quantize(Decimal(1))
        else:
            rounded = value.quantize(Decimal("0.01"))
        text = f"{rounded.copy_abs():f}"

    if rounded == 0 and value != 0:
        return f"<{currency_symbol}0.01"
    whole, _, fraction = text.partition(".")
    body = f"{int(whole):,}" + (f".{fraction}" if fraction else "")
    return f"{'-' if rounded < 0 else ''}{currency_symbol}{body}"


def format_money_short(value: Decimal | None, *, currency_symbol: str = "$") -> str | None:
    """Money at a glance: `$35.7k`, `$123.5k`, `$29.8m`. Precision here would be
    noise — the exact figure lives one click away in the summary."""
    if value is None:
        return None
    with localcontext() as context:
        context.prec = 40
        magnitude = value.copy_abs()
        for threshold, suffix in ((10**9, "b"), (10**6, "m"), (10**3, "k")):
            if magnitude >= threshold:
                scaled = (magnitude / threshold).quantize(Decimal("0.1"))
                text = f"{scaled:f}".removesuffix(".0")
                return f"{'-' if value < 0 else ''}{currency_symbol}{text}{suffix}"
        whole = magnitude.quantize(Decimal(1))
        if whole == 0 and magnitude > 0:
            return f"<{currency_symbol}1"
        return f"{'-' if value < 0 else ''}{currency_symbol}{int(whole):,}"


def relative_time(timestamp: int | None, *, now: int | None = None) -> str:
    if not timestamp:
        return "never"
    delta = max(0, int(now if now is not None else time.time()) - int(timestamp))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _local(timestamp: int) -> datetime:
    """Render in the server's timezone: on a personal VPS that is the timezone
    the reader actually lives in, which is what makes Today/Yesterday useful.
    Set TZ in the container to control it."""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone()


def day_label(day: date, *, today: date | None = None) -> str:
    """'Today', 'Yesterday', 'Sep 1', or 'Sep 1, 2025' for another year."""
    today = today or datetime.now().astimezone().date()
    delta = (today - day).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    if day.year == today.year:
        return f"{day:%b} {day.day}"
    return f"{day:%b} {day.day}, {day.year}"


@dataclass(slots=True)
class Party:
    """One side of a transfer as the UI needs it."""

    address: str
    display: str      # label, wallet name, or shortened address
    short: str        # the shortened address, always
    title: str        # full address, for the tooltip
    url: str
    is_monitored: bool
    is_labelled: bool


@dataclass(slots=True)
class ActivityView:
    id: int
    chain_id: str
    kind: str
    direction: str  # in | out | between | self
    timestamp: int
    time_text: str
    day_key: str        # stable key for day grouping, e.g. "2026-09-03"
    day_text: str       # "Today" / "Yesterday" / "Sep 1"
    iso: str
    wallet: str
    counterparty: Party | None
    source: Party
    target: Party
    amount_text: str
    symbol: str
    asset_url: str | None
    kind_badge: str | None   # only set when the kind is not obvious from the row
    kind_note: str | None    # the same fact in words, for the inspector
    value_text: str | None   # what the amount was worth, when the asset is priced
    value_usd: Decimal | None  # the same, unformatted, for ranking
    tx_hash: str
    tx_short: str
    tx_url: str
    block_number: int
    block_url: str
    # Decimals unknown: amount_text is raw on-chain units, and nothing is valued.
    raw_units: bool = False

    @property
    def cursor(self) -> tuple[int, int]:
        return (self.timestamp, self.id)


def _party(
    chain: Chain,
    address: str,
    account_name: str | None,
    label: str | None,
) -> Party:
    if not address:
        return Party("", "contract creation", "", "no recipient", "", False, False)

    # Your names win. A wallet you monitor, then a label you wrote, and only
    # then what the chain is commonly known to call the address — these are a
    # fallback for counterparties nobody has named, never an override of one.
    known = getattr(chain, "known_label", None)
    if account_name:
        display, labelled = account_name, True
    elif label:
        display, labelled = label, True
    elif known and (name := known(address)):
        display, labelled = name, True
    else:
        display, labelled = chain.shorten_address(address), False
    return Party(
        address=address,
        display=display,
        short=chain.shorten_address(address),
        title=chain.display_address(address),
        url=chain.explorer_address_url(address),
        is_monitored=bool(account_name),
        is_labelled=labelled,
    )


def _token_url(chain: Chain, contract_address: str) -> str | None:
    """Explorer link for a token contract, when the chain exposes one."""
    if contract_address == NATIVE:
        return None
    builder = getattr(chain, "explorer_token_url", None)
    return builder(contract_address) if builder else None


def build_activity(
    row: sqlite3.Row,
    chain: Chain,
    scope: set[int] | None = None,
    prices: dict[int, sqlite3.Row] | None = None,
    excluded: frozenset[int] = frozenset(),
) -> ActivityView:
    """Render one transfer from the point of view of the current scope.

    ``excluded`` are assets whose quote the portfolio refused to believe; a
    row of one is not given the value the total was protected from.

    A movement between two known accounts collapses to ``Main → Cold`` only when
    both ends are inside the scope being viewed — that is the case where it would
    otherwise show up twice. Narrow to one of them and the same row reads as an
    ordinary IN or OUT with the other side as the counterparty, which is what
    someone looking at a single wallet wants to see. ``scope`` of ``None`` means
    every monitored account is in view.
    """
    source = _party(chain, row["from_address"], row["from_account_name"], row["from_label"])
    target = _party(chain, row["to_address"], row["to_account_name"], row["to_label"])

    from_id, to_id = row["from_account_id"], row["to_account_id"]
    in_scope = (lambda account_id: account_id is not None) if scope is None else scope.__contains__

    if from_id is not None and to_id is not None and in_scope(from_id) and in_scope(to_id):
        direction = "self" if from_id == to_id else "between"
        wallet = row["from_account_name"]
        counterparty = None
    elif from_id is not None and in_scope(from_id):
        direction, wallet, counterparty = "out", row["from_account_name"], target
    elif to_id is not None and in_scope(to_id):
        direction, wallet, counterparty = "in", row["to_account_name"], source
    elif from_id is not None:
        direction, wallet, counterparty = "out", row["from_account_name"], target
    else:
        direction, wallet, counterparty = "in", row["to_account_name"], source

    timestamp = row["block_timestamp"]
    moment = _local(timestamp)
    contract = row["contract_address"]

    # A native transfer is the default and a token transfer is evident from its
    # symbol; only an internal transfer tells the reader something they cannot
    # infer, so it is the only kind that earns a badge.
    # "int" is short enough for a dense row and opaque on its own; the words
    # go where there is room to read them.
    kind_badge = "int" if row["kind"] == "internal" else None
    kind_note = (
        "internal — ETH moved by the contract this transaction called, not by a"
        " transfer you signed"
        if row["kind"] == "internal"
        else None
    )

    # Valued at the current price, not the price at the time: this is a monitor,
    # not a ledger. It answers "how much is that" for a row you are looking at.
    value_usd = None
    if prices and row["asset_id"] not in excluded:
        value_usd = value_of(row["amount_raw"], row["decimals"], prices.get(row["asset_id"]))
    value_text = format_money(value_usd)

    return ActivityView(
        id=row["id"],
        chain_id=row["chain_id"],
        kind=row["kind"],
        direction=direction,
        timestamp=timestamp,
        time_text=moment.strftime("%H:%M"),
        day_key=moment.strftime("%Y-%m-%d"),
        day_text=day_label(moment.date()),
        iso=moment.isoformat(),
        wallet=wallet or "",
        counterparty=counterparty,
        source=source,
        target=target,
        amount_text=format_amount(row["amount_raw"], row["decimals"]),
        symbol=row["symbol"],
        asset_url=_token_url(chain, contract),
        kind_badge=kind_badge,
        kind_note=kind_note,
        value_text=value_text,
        value_usd=value_usd,
        tx_hash=row["tx_hash"],
        tx_short=chain.shorten_tx_hash(row["tx_hash"]),
        tx_url=chain.explorer_tx_url(row["tx_hash"]),
        block_number=row["block_number"],
        block_url=chain.explorer_block_url(row["block_number"]),
        raw_units=row["decimals"] is None,
    )


@dataclass(slots=True)
class BalanceView:
    symbol: str
    amount_text: str
    is_native: bool
    url: str | None


@dataclass(slots=True)
class WalletView:
    """One row of the sidebar. Deliberately thin: the sidebar answers *where to
    look*, and the summary panel above answers *what is in there*."""

    id: int
    name: str
    address: str
    address_full: str
    url: str
    value_text: str | None       # abbreviated, for the row
    value_exact: str | None      # full, for the tooltip
    selected: bool
    is_owned: bool
    editable: bool               # only what the UI created can the UI change


def build_wallets(
    accounts,
    balances: dict[int, list[sqlite3.Row]],
    chain: Chain,
    *,
    selected_id: int | None = None,
    prices: dict[int, sqlite3.Row] | None = None,
    relevance: Relevance | None = None,
    max_share: float = 0.9,
) -> list[WalletView]:
    # Valued exactly as the panel values it, with the verdicts made once for
    # the whole group. Two different sums for the same wallet — one here, one
    # in the summary — is a bug the reader has no way to resolve, and so is a
    # list of wallets that does not add up to the total above it.
    verdicts = group_verdicts(accounts, balances, prices or {}, relevance or Relevance(), max_share)
    views: list[WalletView] = []

    for account in accounts:
        holdings = aggregate_holdings([account], balances)
        total = counted_value(holdings, prices or {}, verdicts[account.is_owned]).total

        views.append(
            WalletView(
                id=account.id,
                name=account.name,
                address=chain.display_address(account.address),
                address_full=chain.display_address(account.address),
                url=chain.explorer_address_url(account.address),
                value_text=format_money_short(total),
                value_exact=format_money(total),
                selected=account.id == selected_id,
                is_owned=account.is_owned,
                # The config file owns what it declares and would overwrite an
                # edit made here on the next start.
                editable=account.source == "ui",
            )
        )
    return views


@dataclass(slots=True)
class SummaryView:
    """The one line of aggregate the top bar carries. Deliberately not a
    dashboard: a count and the native total, no fiat, no windows, no charts."""

    account_count: int      # accounts that are mine
    monitored_count: int    # every account being indexed, watched ones included
    native_total: str
    native_symbol: str

    @property
    def count_text(self) -> str:
        """"3 mine · 1 watching" rather than "3 of 4 accounts": the arithmetic is
        the same, but this says which number the total belongs to."""
        watching = self.monitored_count - self.account_count
        if not watching:
            noun = "account" if self.account_count == 1 else "accounts"
            return f"{self.account_count} {noun}"
        return f"{self.account_count} mine · {watching} watching"


def build_summary(accounts, balances: dict[int, list[sqlite3.Row]], chain: Chain) -> SummaryView:
    # The total is over owned accounts only. Adding a watched whale's balance
    # into the same figure would make the number mean nothing.
    owned = [account for account in accounts if account.is_owned]
    total = 0
    for account in owned:
        for row in balances.get(account.id, []):
            if row["contract_address"] == NATIVE:
                total += int(row["amount_raw"])
    return SummaryView(
        account_count=len(owned),
        monitored_count=len(accounts),
        native_total=format_amount(total, chain.native_decimals, max_dp=4),
        native_symbol=chain.native_symbol,
    )


@dataclass(slots=True)
class StatusView:
    chain_name: str
    chain_id: str
    state: str  # ok | stale | error | starting
    head_block: int | None
    head_block_text: str
    updated_text: str
    detail: str
    activity_count: int
    backfilling: bool


def build_status(
    status: SyncStatus,
    chain: Chain,
    *,
    stale_after: int,
    now: int | None = None,
) -> StatusView:
    now = now if now is not None else int(time.time())
    age = None if status.last_success_at is None else now - status.last_success_at

    if status.last_error:
        state, detail = "error", status.last_error
    elif status.last_success_at is None:
        state = "starting"
        detail = "building the initial index — this can take a minute"
    elif age is not None and age > stale_after:
        state = "stale"
        detail = f"no successful sync for {relative_time(status.last_success_at, now=now)}"
    elif age is not None and age > stale_after // 3:
        # Not yet a problem worth words, but the dot can say it without one.
        state, detail = "lagging", ""
    else:
        state, detail = "ok", ""

    return StatusView(
        chain_name=chain.display_name,
        chain_id=chain.chain_id,
        state=state,
        head_block=status.head_block,
        head_block_text=f"{status.head_block:,}" if status.head_block else "—",
        updated_text=relative_time(status.last_success_at, now=now),
        detail=detail,
        activity_count=status.activity_count,
        backfilling=not status.backfill_done,
    )


# --------------------------------------------------------------- portfolio


@dataclass(slots=True)
class AssetTotal:
    asset_id: int
    symbol: str
    amount_text: str
    value_text: str | None
    is_native: bool
    url: str | None
    excluded: bool = False
    hidden: bool = False     # indexed and kept, just not worth showing by default
    raw_units: bool = False  # decimals unknown: the amount is raw on-chain units


def value_of(amount_raw: int | str, decimals: int | None, quote) -> Decimal | None:
    """What an amount is worth, or ``None`` when that cannot honestly be said:
    no price, or decimals the chain would not state. Unknown is never zero."""
    if quote is None or decimals is None:
        return None
    with localcontext() as context:
        context.prec = 40
        return Decimal(int(amount_raw)).scaleb(-decimals) * Decimal(quote["price"])


@dataclass(frozen=True, slots=True)
class Holding:
    """One asset's balance summed over some accounts."""

    asset_id: int
    symbol: str
    contract: str
    decimals: int | None
    raw: int

    @property
    def is_native(self) -> bool:
        return self.contract == NATIVE


def aggregate_holdings(accounts, balances: dict[int, list[sqlite3.Row]]) -> dict[int, Holding]:
    raw: dict[int, int] = {}
    first: dict[int, sqlite3.Row] = {}
    for account in accounts:
        for row in balances.get(account.id, []):
            raw[row["asset_id"]] = raw.get(row["asset_id"], 0) + int(row["amount_raw"])
            first.setdefault(row["asset_id"], row)
    return {
        asset_id: Holding(
            asset_id=asset_id,
            symbol=first[asset_id]["symbol"],
            contract=first[asset_id]["contract_address"],
            decimals=first[asset_id]["decimals"],
            raw=amount,
        )
        for asset_id, amount in raw.items()
        if amount > 0
    }


@dataclass(frozen=True, slots=True)
class Verdicts:
    """Which held assets count towards a total. Decided once per group of
    accounts and applied to every view of it — the sidebar row, the panel,
    one wallet or all of them — so they cannot disagree."""

    hidden: frozenset[int] = frozenset()    # dust and the unvouched: not shown
    excluded: frozenset[int] = frozenset()  # shown, but the quote is not believed


def _implausible(
    values: dict[int, Decimal], holdings: dict[int, Holding], rules: Relevance, max_share: float
) -> set[int]:
    """Assets whose value is too large to believe, given what they are.

    The case this exists for is a scam token with a nominal DEX quote and a
    supply of 10^17, which made a real portfolio read $4.9 quadrillion. The case
    it must *not* touch is a wallet that simply holds one thing: a stablecoin
    balance is often the whole of it, and calling that implausible is worse than
    the problem — so only assets nothing vouches for are ever candidates.
    """
    if not max_share or len(values) < 2:
        return set()
    candidates = sorted(
        (
            (value, asset_id)
            for asset_id, value in values.items()
            if not holdings[asset_id].is_native
            and not rules.is_vouched_for(asset_id, holdings[asset_id].contract)
        ),
        reverse=True,
    )
    excluded: set[int] = set()
    remaining = sum(values.values(), Decimal(0))
    for value, asset_id in candidates:
        if remaining > 0 and value / remaining > Decimal(str(max_share)):
            excluded.add(asset_id)
            remaining -= value
    return excluded


def judge_holdings(
    holdings: dict[int, Holding], prices, rules: Relevance, max_share: float
) -> Verdicts:
    """Hidden first, then implausible among what is left: a forged ticker or a
    speck of dust must not count towards the share a scam quote is measured
    against."""
    values = {h.asset_id: value_of(h.raw, h.decimals, prices.get(h.asset_id))
              for h in holdings.values()}
    hidden = {
        asset_id
        for asset_id, holding in holdings.items()
        if not rules.is_relevant(asset_id, holding.contract, values[asset_id])
    }
    shown_priced = {
        asset_id: value
        for asset_id, value in values.items()
        if value is not None and asset_id not in hidden
    }
    return Verdicts(
        hidden=frozenset(hidden),
        excluded=frozenset(_implausible(shown_priced, holdings, rules, max_share)),
    )


def group_verdicts(
    accounts, balances, prices, rules: Relevance, max_share: float
) -> dict[bool, Verdicts]:
    """Verdicts for my accounts (``True``) and for the watched ones (``False``),
    each judged over its whole group."""
    return {
        owned: judge_holdings(
            aggregate_holdings([a for a in accounts if a.is_owned == owned], balances),
            prices, rules, max_share,
        )
        for owned in (True, False)
    }


@dataclass(frozen=True, slots=True)
class Counted:
    total: Decimal | None
    native: Decimal | None
    tokens: Decimal
    any_token_priced: bool


def counted_value(holdings: dict[int, Holding], prices, verdicts: Verdicts) -> Counted:
    """The one sum both the sidebar and the panel show: what is priced, has
    known decimals, and is neither hidden nor excluded. ``total`` is ``None``
    when nothing at all could be valued — unknown, not zero."""
    native: Decimal | None = None
    tokens = Decimal(0)
    any_token = False
    # Summed in a wide context: the default 28 digits rounds a large sum, and
    # the old add-then-subtract of an absurd quote lost the dollars below it.
    with localcontext() as context:
        context.prec = 80
        for holding in holdings.values():
            if holding.asset_id in verdicts.hidden or holding.asset_id in verdicts.excluded:
                continue
            value = value_of(holding.raw, holding.decimals, prices.get(holding.asset_id))
            if value is None:
                continue
            if holding.is_native:
                native = value
            else:
                tokens += value
                any_token = True
        total = None if native is None and not any_token else (native or Decimal(0)) + tokens
    return Counted(total=total, native=native, tokens=tokens, any_token_priced=any_token)


@dataclass(slots=True)
class PortfolioView:
    """What the account (or all accounts together) currently holds.

    Answers "how much do I have?" next to the feed's "what happened?". Values
    are omitted rather than guessed: an unpriced asset counts as unknown, never
    as zero, and the view says how many it could not value.
    """

    scope: str
    scope_meta: str    # the shortened address, when the scope is one wallet
    scope_address: str # ...and the full one, for copying
    scope_count: str   # "3 accounts", when the scope is all of them
    scope_badge: str   # "my wallet" / "watching", when the scope is one wallet
    account_count: int
    total_text: str | None
    native_amount_text: str
    native_value_text: str | None
    native_symbol: str
    token_value_text: str | None
    token_count: int
    assets: list[AssetTotal]
    hidden_assets: int
    priced: bool
    unpriced: int            # worth showing, but no price for it
    hidden: int              # dust and unsolicited tokens, kept but not shown
    excluded: list[str]      # priced, but too implausible to add up
    price_age_text: str | None
    prices_stale: bool


@dataclass(slots=True)
class Relevance:
    """Which token holdings are worth a reader's attention.

    Never balance: token supplies are arbitrary, so "1" of something says
    nothing — 1 WBTC and 1 scam coin look identical as a number. A holding earns
    its place by being worth something, by being a token this chain is known to
    use, by having been *sent* by one of your own wallets, or by your saying so.
    Everything else is kept and indexed, and simply not shown by default.
    """

    known: frozenset[str] = frozenset()      # contracts the chain is known for
    trusted: frozenset[str] = frozenset()    # contracts the user vouched for
    sent_asset_ids: frozenset[int] = frozenset()
    impostor_asset_ids: frozenset[int] = frozenset()
    dust_below: Decimal = Decimal(1)

    def is_vouched_for(self, asset_id: int, contract: str) -> bool:
        """Something other than its own price says this asset is real.

        "You sent it" is deliberately weaker than it looks: an ERC-20 contract
        can emit a Transfer log with any `from` it likes, including your address,
        and address-poisoning scams do exactly that. So an impostor overrides it.
        """
        if asset_id in self.impostor_asset_ids:
            return False
        if contract in self.trusted:
            return True
        return contract in self.known or asset_id in self.sent_asset_ids

    def is_relevant(self, asset_id: int, contract: str, value: Decimal | None) -> bool:
        if asset_id in self.impostor_asset_ids:
            return False   # a forged ticker is never worth a place, at any value
        if contract == NATIVE:
            return True
        if value is not None and value >= self.dust_below:
            return True
        return self.is_vouched_for(asset_id, contract)


def build_portfolio(
    accounts,
    balances: dict[int, list[sqlite3.Row]],
    prices: dict[int, sqlite3.Row],
    chain: Chain,
    *,
    selected_id: int | None = None,
    watched_scope: bool = False,
    max_assets: int = 8,
    stale_after: int = 3600,
    max_share: float = 0.9,
    relevance: Relevance | None = None,
    now: int | None = None,
) -> PortfolioView:
    """Totals for the scope in view.

    Summing a watched whale's balance into "my total" would be nonsense, so the
    default is the owned accounts however wide the *feed* is set. But choosing
    the watched group deliberately is a different request, and the panel follows
    it rather than answering a question nobody asked.
    """
    now = now if now is not None else int(time.time())
    if selected_id is not None:
        in_scope = [a for a in accounts if a.id == selected_id]
        group_owned = in_scope[0].is_owned if in_scope else True
    elif watched_scope:
        in_scope = [a for a in accounts if not a.is_owned]
        group_owned = False
    else:
        in_scope = [a for a in accounts if a.is_owned]
        group_owned = True

    rules = relevance or Relevance()
    # Judged over the whole group, not this view of it, so one wallet's panel
    # and its sidebar row — and the wallets and their total — agree.
    verdicts = group_verdicts(accounts, balances, prices, rules, max_share)[group_owned]
    holdings = aggregate_holdings(in_scope, balances)
    counted = counted_value(holdings, prices, verdicts)

    priced_at: int | None = None
    native_amount_text = "0"
    entries: list[tuple[Decimal | None, AssetTotal]] = []
    for holding in holdings.values():
        if quote := prices.get(holding.asset_id):
            priced_at = max(priced_at or 0, quote["updated_at"])
        excluded = holding.asset_id in verdicts.excluded
        # An excluded asset's quote is the one figure the app refused to
        # believe, and a forged ticker's quote is a pool the forger made;
        # printing either beside the asset would hand it back.
        untrusted = excluded or holding.asset_id in rules.impostor_asset_ids
        value = None if untrusted else value_of(
            holding.raw, holding.decimals, prices.get(holding.asset_id)
        )
        amount_text = format_amount(holding.raw, holding.decimals, max_dp=4)
        if holding.is_native:
            native_amount_text = amount_text
        entries.append(
            (
                value,
                AssetTotal(
                    asset_id=holding.asset_id,
                    symbol=holding.symbol,
                    amount_text=amount_text,
                    value_text=format_money(value),
                    is_native=holding.is_native,
                    url=_token_url(chain, holding.contract),
                    excluded=excluded,
                    hidden=holding.asset_id in verdicts.hidden,
                    raw_units=holding.decimals is None,
                ),
            )
        )

    # Shown first, largest holding first; then the unpriced, then what the
    # total refused to believe; hidden dust last, so revealing it never
    # rearranges what was already on screen.
    entries.sort(
        key=lambda item: (
            item[1].hidden, item[1].excluded, item[0] is None, -(item[0] or Decimal(0)),
            item[1].symbol,
        )
    )
    assets = [asset for _, asset in entries]
    visible = [asset for asset in assets if not asset.hidden]
    credible = [asset for asset in visible if not asset.excluded]
    hidden = len(assets) - len(visible)
    excluded = [asset.symbol for asset in assets if asset.excluded]
    total = counted.total

    if selected_id is None:
        scope = "Watching" if watched_scope else "My wallets"
        scope_meta = ""
        scope_address = ""
        scope_count = f"{len(in_scope)} account{'' if len(in_scope) == 1 else 's'}"
        # Never let an aggregate of other people's addresses read as a portfolio.
        scope_badge = "not mine" if watched_scope else ""
    else:
        account = next((a for a in in_scope), None)
        scope = account.name if account else ""
        # An address is worth showing when the panel is open and worth dropping
        # from the one-line form, where the value is the point.
        scope_meta = chain.shorten_address(account.address) if account else ""
        scope_address = chain.display_address(account.address) if account else ""
        scope_count = ""
        # Opening a watched address should never leave any doubt that its value
        # is not part of the portfolio.
        scope_badge = "" if account is None else ("my wallet" if account.is_owned else "watching")

    return PortfolioView(
        scope=scope,
        scope_meta=scope_meta,
        scope_address=scope_address,
        scope_count=scope_count,
        scope_badge=scope_badge,
        account_count=len(in_scope),
        total_text=format_money(total),
        native_amount_text=native_amount_text,
        native_value_text=format_money(counted.native),
        native_symbol=chain.native_symbol,
        token_value_text=format_money(counted.tokens) if counted.any_token_priced else None,
        token_count=sum(1 for a in visible if not a.is_native),
        # An excluded asset sorts last but is never cut: it is named in the
        # total's note, and the list must show what that note refers to.
        assets=(credible[:max_assets] + [a for a in visible if a.excluded]
                + [a for a in assets if a.hidden]),
        hidden_assets=max(0, len(credible) - max_assets),
        priced=total is not None,
        unpriced=sum(1 for a in visible if a.value_text is None and not a.excluded),
        hidden=hidden,
        excluded=excluded,
        price_age_text=relative_time(priced_at, now=now) if priced_at else None,
        prices_stale=bool(priced_at and now - priced_at > stale_after),
    )


# --------------------------------------------------------------- inspector


@dataclass(slots=True)
class TransactionView:
    """One Ethereum transaction and every movement it produced.

    The feed is a list of transfers, which is the right unit for scanning: a
    swap really did move four different things. But those four rows share one
    cause, and the feed cannot say so without becoming a tree. So the cause is
    a place you can go to, and this is it — the amounts stay separate, because
    adding a token to an unrelated token is not a sum anyone asked for.
    """

    chain_id: str
    chain_name: str
    tx_hash: str
    tx_short: str
    tx_url: str
    block_number: int
    block_text: str
    block_url: str
    day_text: str
    time_text: str
    iso: str
    age_text: str
    confirmations: int | None
    confirmations_text: str
    transfers: list[ActivityView]
    selected_id: int
    fee_text: str | None = None        # what it cost, in the native asset
    fee_value_text: str | None = None  # and in money, when the asset is priced
    gas_text: str | None = None        # used / limit
    succeeded: bool | None = None

    @property
    def count(self) -> int:
        return len(self.transfers)


def build_transaction(
    rows: list[sqlite3.Row],
    chain: Chain,
    *,
    selected_id: int,
    scope: set[int] | None = None,
    prices: dict[int, sqlite3.Row] | None = None,
    head_block: int | None = None,
    now: int | None = None,
    cost: TransactionCost | None = None,
    native_price: Decimal | None = None,
    excluded: frozenset[int] = frozenset(),
) -> TransactionView:
    transfers = [build_activity(row, chain, scope, prices, excluded) for row in rows]
    first = transfers[0]

    depth = None if not head_block else max(0, head_block - first.block_number + 1)

    # Absent throughout when the chain could not say: a fee shown as zero is a
    # claim, and the wrong one.
    fee_text = fee_value_text = gas_text = None
    if cost is not None:
        with localcontext() as context:
            context.prec = 40
            fee = Decimal(cost.fee_raw).scaleb(-chain.native_decimals)
            fee_text = (
                f"{format_amount(cost.fee_raw, chain.native_decimals)}"
                f" {chain.native_symbol}"
            )
            if native_price is not None:
                fee_value_text = format_money(fee * native_price)
        gas_text = f"{cost.gas_used:,}"
        if cost.gas_limit:
            gas_text += f" / {cost.gas_limit:,}"

    return TransactionView(
        chain_id=first.chain_id,
        chain_name=chain.display_name,
        tx_hash=first.tx_hash,
        tx_short=first.tx_short,
        tx_url=first.tx_url,
        block_number=first.block_number,
        block_text=f"{first.block_number:,}",
        block_url=first.block_url,
        day_text=first.day_text,
        time_text=first.time_text,
        iso=first.iso,
        age_text=relative_time(first.timestamp, now=now),
        confirmations=depth,
        confirmations_text="—" if depth is None else f"{depth:,}",
        transfers=transfers,
        selected_id=selected_id,
        fee_text=fee_text,
        fee_value_text=fee_value_text,
        gas_text=gas_text,
        succeeded=None if cost is None else cost.succeeded,
    )


# ------------------------------------------------------------- grouping


@dataclass(slots=True)
class TransactionGroup:
    """The transfers of one transaction, as one entry in the feed.

    A swap is one thing that happened and four rows in the index, and four rows
    minutes apart in the feed is how a feed misleads. They are still four rows —
    the amounts are not ours to add together, and an unrelated token added to
    another is not a sum — but they arrive as one entry, led by the transfer
    that carries the most value, with the rest folded behind it.
    """

    primary: ActivityView
    siblings: list[ActivityView]

    @property
    def is_group(self) -> bool:
        return bool(self.siblings)

    @property
    def extra(self) -> int:
        return len(self.siblings)

    @property
    def rows(self) -> list[ActivityView]:
        return [self.primary, *self.siblings]


def group_by_transaction(activities: list[ActivityView]) -> list[TransactionGroup]:
    """Fold each transaction's transfers into one entry, in feed order.

    The group keeps the position of its first transfer, so nothing jumps up the
    page. Which transfer leads is decided by value, because that is the one a
    reader is looking for; with nothing priced, on-chain order decides, since
    guessing between two unpriced amounts of different assets is worse than not
    guessing at all.
    """
    order: list[str] = []
    by_tx: dict[str, list[ActivityView]] = {}
    for activity in activities:
        if activity.tx_hash not in by_tx:
            order.append(activity.tx_hash)
            by_tx[activity.tx_hash] = []
        by_tx[activity.tx_hash].append(activity)

    groups = []
    for tx_hash in order:
        rows = by_tx[tx_hash]
        lead = max(range(len(rows)), key=lambda i: (rows[i].value_usd or Decimal(0), -i))
        groups.append(TransactionGroup(rows[lead], [r for i, r in enumerate(rows) if i != lead]))
    return groups
