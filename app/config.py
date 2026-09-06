"""Configuration: a YAML file for what to watch, environment for how to run.

The YAML schema carries an explicit ``chain`` per wallet. Only ``ethereum`` is
accepted today; the key exists so that adding a chain does not mean rewriting
everyone's config file.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml

from app.chains import UnknownChainError, get_chain, supported_chains

DEFAULT_CHAIN = "ethereum"


class ConfigError(Exception):
    """Raised with every problem found, so one restart surfaces them all."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("\n".join(f"  - {p}" for p in problems))


@dataclass(frozen=True, slots=True)
class WalletConfig:
    name: str
    chain_id: str
    address: str  # canonical form
    is_owned: bool = True


@dataclass(frozen=True, slots=True)
class WatchConfig:
    wallets: tuple[WalletConfig, ...]
    labels: dict[tuple[str, str], str] = field(default_factory=dict)
    ignore_assets: frozenset[tuple[str, str]] = frozenset()
    # Contracts you vouch for: shown even when worth nothing and unrecognised.
    trusted_assets: frozenset[tuple[str, str]] = frozenset()

    @property
    def chain_ids(self) -> list[str]:
        return sorted({w.chain_id for w in self.wallets})


@dataclass(frozen=True, slots=True)
class Settings:
    config_path: Path
    database_path: Path
    alchemy_url: str
    alchemy_api_key: str
    sync_interval: int
    backfill_days: int
    reorg_depth: int
    max_token_balances: int
    max_token_lookups: int
    provider_concurrency: int
    stale_after: int
    price_refresh: int
    balance_refresh: int
    prices_enabled: bool
    value_max_share: float
    dust_below_usd: Decimal
    dust_transfer_usd: Decimal
    prices_base_url: str
    host: str
    port: int
    log_level: str
    auth_user: str
    auth_password: str

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_user and self.auth_password)

    @property
    def prices_available(self) -> bool:
        """Valuation needs the raw key: the Prices API is a different host from
        the JSON-RPC endpoint, so a bare ALCHEMY_URL cannot reach it."""
        return self.prices_enabled and bool(self.alchemy_api_key)

    @property
    def backfill_blocks(self) -> int:
        # Ethereum settles a block roughly every 12s; good enough to turn a
        # human-friendly "days" knob into a starting block.
        return max(0, self.backfill_days * 24 * 60 * 60 // 12)


def _env_int(env: Mapping[str, str], name: str, default: int, *, minimum: int = 0) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        raise ConfigError([f"{name} must be an integer, got {raw!r}"]) from None


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Read settings from a mapping, defaulting to the process environment.

    Nothing here writes back to ``os.environ``: settings are a value, and tests
    (or a future second instance) must be able to build one without changing the
    process they run in.
    """
    env = os.environ if env is None else env

    key = env.get("ALCHEMY_API_KEY", "").strip()
    url = env.get("ALCHEMY_URL", "").strip()
    if not url:
        if not key:
            raise ConfigError(
                ["ALCHEMY_API_KEY is not set (or set ALCHEMY_URL to a full provider URL)"]
            )
        from app.chains.ethereum.alchemy import build_url

        url = build_url(key)

    # 90s keeps a handful of wallets inside a free provider tier; see the
    # compute-unit budget in README.md before lowering it.
    interval = _env_int(env, "SYNC_INTERVAL_SECONDS", 90, minimum=5)
    return Settings(
        config_path=Path(env.get("BLOCKTAIL_CONFIG", "wallets.yml")),
        database_path=Path(env.get("BLOCKTAIL_DB", "data/blocktail.db")),
        alchemy_url=url,
        alchemy_api_key=key,
        sync_interval=interval,
        backfill_days=_env_int(env, "BACKFILL_DAYS", 90, minimum=0),
        reorg_depth=_env_int(env, "REORG_DEPTH", 64, minimum=0),
        max_token_balances=_env_int(env, "MAX_TOKEN_BALANCES", 10, minimum=0),
        # An exchange address can hold thousands of tokens; each needs a
        # metadata lookup, so the number of them is bounded.
        max_token_lookups=_env_int(env, "MAX_TOKEN_LOOKUPS", 200, minimum=1),
        # A free provider tier caps compute per second, not just per month. Two
        # requests in flight stays inside it; raise this on a paid plan.
        provider_concurrency=_env_int(env, "PROVIDER_CONCURRENCY", 2, minimum=1),
        stale_after=_env_int(env, "STALE_AFTER_SECONDS", max(180, interval * 3), minimum=30),
        price_refresh=_env_int(env, "PRICE_REFRESH_SECONDS", 300, minimum=30),
        balance_refresh=_env_int(env, "BALANCE_REFRESH_SECONDS", 600, minimum=0),
        # A scam token can have a nominal DEX quote and an absurd supply, which
        # is enough to swallow a portfolio total whole. 0 disables the guard.
        value_max_share=min(1.0, _env_int(env, "VALUE_MAX_SHARE_PERCENT", 90, minimum=0) / 100),
        # Below this a token has to earn its place some other way — being known,
        # being vouched for, or having been sent by one of your own wallets.
        dust_below_usd=Decimal(_env_int(env, "DUST_BELOW_USD_CENTS", 100, minimum=0)) / 100,
        # A transfer worth less than this is noise in a log. Priced assets only:
        # 0.000059 USDT and 0.000059 WBTC are not the same event.
        dust_transfer_usd=Decimal(_env_int(env, "DUST_TRANSFER_USD_CENTS", 1, minimum=0)) / 100,
        prices_enabled=env.get("PRICES", "on").strip().lower() not in {"off", "0", "false", "no"},
        prices_base_url=env.get("ALCHEMY_PRICES_URL", "https://api.g.alchemy.com/prices/v1").strip(),
        auth_user=env.get("AUTH_USER", "").strip(),
        auth_password=env.get("AUTH_PASSWORD", ""),
        host=env.get("HOST", "0.0.0.0"),
        port=_env_int(env, "PORT", 8000, minimum=1),
        log_level=env.get("LOG_LEVEL", "INFO").upper(),
    )


def load_watch_config(path: Path) -> WatchConfig:
    if path.is_dir():
        # Docker creates a directory when a bind-mounted file is missing on the
        # host, which is otherwise a baffling crash.
        raise ConfigError(
            [
                f"{path} is a directory, not a file"
                " — create wallets.yml on the host before starting the container"
                " (cp wallets.example.yml wallets.yml)"
            ]
        )
    if not path.exists():
        raise ConfigError([f"config file {path} does not exist"])

    try:
        text = path.read_text()
    except PermissionError:
        # The container runs unprivileged, so a 0600 file owned by root on the
        # host is unreadable inside it. Worth saying plainly: as a traceback at
        # startup this costs an afternoon.
        raise ConfigError(
            [
                f"{path} cannot be read. It is mounted from the host, and blocktail"
                " runs as an unprivileged user inside the container — give the file"
                " world-read permission (chmod 644 wallets.yml). It holds addresses,"
                " not credentials; keep .env at 600."
            ]
        ) from None
    except OSError as exc:
        raise ConfigError([f"{path} cannot be read: {exc}"]) from None

    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError([f"{path} is not valid YAML: {exc}"]) from None
    if not isinstance(raw, dict):
        raise ConfigError([f"{path} must contain a YAML mapping at the top level"])

    return parse_watch_config(raw, source=str(path))


def parse_watch_config(raw: dict, *, source: str = "config") -> WatchConfig:
    problems: list[str] = []
    default_chain = str(raw.get("chain") or DEFAULT_CHAIN)

    wallets: list[WalletConfig] = []
    seen: dict[tuple[str, str], str] = {}
    # "accounts" is accepted as a synonym: the domain calls them accounts, the
    # UI calls them wallets, and a config file should not have to care.
    entries = raw.get("wallets", raw.get("accounts"))
    if not isinstance(entries, list) or not entries:
        problems.append(f"{source}: 'wallets' must be a non-empty list")
        entries = []

    for index, entry in enumerate(entries, start=1):
        where = f"{source}: wallets[{index}]"
        if not isinstance(entry, dict):
            problems.append(f"{where} must be a mapping with 'name' and 'address'")
            continue

        name = str(entry.get("name") or "").strip()
        if not name:
            problems.append(f"{where} is missing 'name'")

        chain_id = str(entry.get("chain") or default_chain).strip().lower()
        try:
            chain = get_chain(chain_id)
        except UnknownChainError as exc:
            problems.append(f"{where}: {exc}")
            continue

        try:
            address = chain.normalize_address(str(entry.get("address") or ""))
        except ValueError as exc:
            problems.append(f"{where} ({name or 'unnamed'}): {exc}")
            continue

        key = (chain_id, address)
        if key in seen:
            problems.append(
                f"{where} ({name}): address already watched as {seen[key]!r}"
            )
            continue
        owned = entry.get("owned", True)
        if not isinstance(owned, bool):
            problems.append(f"{where} ({name}): 'owned' must be true or false")
            continue

        seen[key] = name
        if name:
            wallets.append(
                WalletConfig(name=name, chain_id=chain_id, address=address, is_owned=owned)
            )

    labels: dict[tuple[str, str], str] = {}
    raw_labels = raw.get("labels") or {}
    if not isinstance(raw_labels, dict):
        problems.append(f"{source}: 'labels' must be a mapping of address -> label")
        raw_labels = {}
    for raw_address, label in raw_labels.items():
        try:
            chain = get_chain(default_chain)
            address = chain.normalize_address(str(raw_address))
        except (UnknownChainError, ValueError) as exc:
            problems.append(f"{source}: labels[{raw_address!r}]: {exc}")
            continue
        text = str(label or "").strip()
        if not text:
            problems.append(f"{source}: labels[{raw_address!r}] has an empty label")
            continue
        labels[(default_chain, address)] = text

    def _contract_set(key: str) -> set[tuple[str, str]]:
        collected: set[tuple[str, str]] = set()
        entries = raw.get(key) or []
        if not isinstance(entries, list):
            problems.append(f"{source}: '{key}' must be a list of contract addresses")
            return collected
        for raw_address in entries:
            try:
                chain = get_chain(default_chain)
                collected.add((default_chain, chain.normalize_address(str(raw_address))))
            except (UnknownChainError, ValueError) as exc:
                problems.append(f"{source}: {key}: {exc}")
        return collected

    ignore = _contract_set("ignore_assets")
    trusted = _contract_set("trusted_assets")

    if problems:
        raise ConfigError(problems)

    return WatchConfig(
        wallets=tuple(wallets),
        labels=labels,
        ignore_assets=frozenset(ignore),
        trusted_assets=frozenset(trusted),
    )


def supported_chain_list() -> list[str]:
    return supported_chains()
