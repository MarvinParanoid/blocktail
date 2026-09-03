"""Application wiring: configuration -> database -> indexer -> HTTP."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.auth import BasicAuthMiddleware
from app.chains import Chain, ChainDataProvider, get_chain
from app.config import (
    ConfigError,
    Settings,
    WatchConfig,
    load_settings,
    load_watch_config,
    supported_chain_list,
)
from app.db import Database
from app.prices import PriceSource
from app.sync import Indexer
from app.web.routes import router

log = logging.getLogger("blocktail")

STATIC_DIR = Path(__file__).parent / "web" / "static"


@dataclass(slots=True)
class AppContext:
    settings: Settings
    config: WatchConfig
    chain: Chain
    provider: ChainDataProvider
    price_source: PriceSource | None
    db: Database
    indexer: Indexer


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def build_context(
    settings: Settings,
    config: WatchConfig,
    *,
    provider: ChainDataProvider | None = None,
    price_source: PriceSource | None = None,
) -> AppContext:
    chain_ids = config.chain_ids or ["ethereum"]
    if len(chain_ids) > 1:
        # The model is chain-neutral, but one indexer/provider pair is wired up.
        raise ConfigError(
            [
                "this build indexes a single chain per instance; "
                f"the config asks for {', '.join(chain_ids)}"
            ]
        )

    chain = get_chain(chain_ids[0])
    if provider is None:
        from app.chains.ethereum.alchemy import AlchemyProvider

        provider = AlchemyProvider(
            settings.alchemy_url, max_token_lookups=settings.max_token_lookups
        )

    if price_source is None and settings.prices_available:
        from app.prices import AlchemyPrices

        price_source = AlchemyPrices(settings.alchemy_api_key, base_url=settings.prices_base_url)
    elif price_source is None:
        log.info("USD valuation is off (no API key, or PRICES=off)")

    db = Database(settings.database_path)
    indexer = Indexer(
        db=db,
        provider=provider,
        chain=chain,
        config=config,
        settings=settings,
        price_source=price_source,
    )
    return AppContext(
        settings=settings,
        config=config,
        chain=chain,
        provider=provider,
        price_source=price_source,
        db=db,
        indexer=indexer,
    )


def create_app(
    *,
    settings: Settings | None = None,
    config: WatchConfig | None = None,
    provider: ChainDataProvider | None = None,
    price_source: PriceSource | None = None,
    run_indexer: bool = True,
) -> FastAPI:
    settings = settings or load_settings()
    config = config or load_watch_config(settings.config_path)
    ctx = build_context(settings, config, provider=provider, price_source=price_source)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        applied = ctx.db.migrate()
        if applied:
            log.info("applied migrations: %s", ", ".join(map(str, applied)))

        accounts = ctx.db.apply_watch_config(ctx.config)
        # A wallet added to the config after some history was already indexed
        # must pick up that history, so re-derive the account columns.
        ctx.db.relink_accounts()
        log.info(
            "watching %d wallet(s) on %s: %s",
            len(accounts),
            ctx.chain.chain_id,
            ", ".join(a.name for a in accounts) or "none",
        )

        task: asyncio.Task | None = None
        if run_indexer:
            task = asyncio.create_task(ctx.indexer.run_forever(), name="indexer")

        try:
            yield
        finally:
            if task is not None:
                ctx.indexer.stop()
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            await ctx.provider.close()
            if ctx.price_source is not None:
                await ctx.price_source.close()
            ctx.db.close()

    app = FastAPI(
        title="blocktail",
        description="Read-only wallet activity monitor.",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.ctx = ctx
    if settings.auth_enabled:
        app.add_middleware(
            BasicAuthMiddleware,
            username=settings.auth_user,
            password=settings.auth_password,
        )
        log.info("HTTP basic auth is on for user %r", settings.auth_user)
    else:
        log.warning(
            "no AUTH_USER/AUTH_PASSWORD set: this instance is open to anyone who"
            " can reach it — put it behind a proxy or a tailnet"
        )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)
    return app


def main() -> FastAPI:
    """Entry point used by uvicorn (``app.main:main`` with ``--factory``)."""
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"blocktail: configuration error:\n{exc}", file=sys.stderr)
        raise SystemExit(2) from None

    configure_logging(settings.log_level)
    log.info("supported chains: %s", ", ".join(supported_chain_list()))

    # The indexer runs inside the web process, so a second worker means a second
    # indexer racing the first. Storing is idempotent, so this wastes provider
    # quota rather than corrupting anything — but it is never what anyone wants.
    if (os.environ.get("WEB_CONCURRENCY") or "1").strip() not in {"", "1"}:
        log.warning(
            "WEB_CONCURRENCY=%s: blocktail indexes in-process and expects a single"
            " worker; extra workers each run their own indexer",
            os.environ["WEB_CONCURRENCY"],
        )

    try:
        config = load_watch_config(settings.config_path)
    except ConfigError as exc:
        log.error("invalid wallet configuration in %s:\n%s", settings.config_path, exc)
        raise SystemExit(2) from None

    return create_app(settings=settings, config=config)
