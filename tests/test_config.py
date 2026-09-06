"""Configuration parsing and startup validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import ConfigError, load_settings, load_watch_config, parse_watch_config
from tests.conftest import COLD, MAIN

CHECKSUMMED = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"


def test_minimal_config():
    config = parse_watch_config({"wallets": [{"name": "Main", "address": MAIN}]})

    assert len(config.wallets) == 1
    assert config.wallets[0].chain_id == "ethereum", "chain defaults, but is explicit in the model"
    assert config.wallets[0].address == MAIN.lower()
    assert config.chain_ids == ["ethereum"]


def test_explicit_chain_is_accepted():
    config = parse_watch_config(
        {"wallets": [{"name": "Main", "chain": "ethereum", "address": MAIN}]}
    )
    assert config.wallets[0].chain_id == "ethereum"


def test_unknown_chain_is_rejected_with_the_supported_list():
    with pytest.raises(ConfigError) as info:
        parse_watch_config({"wallets": [{"name": "BTC", "chain": "bitcoin", "address": MAIN}]})

    assert "unsupported chain 'bitcoin'" in str(info.value)
    assert "ethereum" in str(info.value)


def test_addresses_are_validated_and_canonicalized():
    config = parse_watch_config({"wallets": [{"name": "Main", "address": CHECKSUMMED}]})
    assert config.wallets[0].address == CHECKSUMMED.lower()


@pytest.mark.parametrize(
    "address",
    [
        "0x123",                                        # too short
        "not-an-address",
        "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",   # valid, but not on this chain
        "0x5aaeb6053F3E94C9b9A09f33669435E7Ef1BeAed",   # broken EIP-55 checksum
        "",
    ],
)
def test_invalid_addresses_are_rejected_at_startup(address):
    with pytest.raises(ConfigError):
        parse_watch_config({"wallets": [{"name": "Bad", "address": address}]})


def test_every_problem_is_reported_at_once():
    with pytest.raises(ConfigError) as info:
        parse_watch_config(
            {
                "wallets": [
                    {"name": "Good", "address": MAIN},
                    {"name": "Bad", "address": "0x123"},
                    {"address": COLD},
                    {"name": "Worse", "chain": "solana", "address": MAIN},
                ]
            }
        )

    assert len(info.value.problems) == 3


def test_duplicate_addresses_are_rejected():
    with pytest.raises(ConfigError, match="already watched"):
        parse_watch_config(
            {"wallets": [{"name": "A", "address": MAIN}, {"name": "B", "address": MAIN.upper().replace("0X", "0x")}]}
        )


def test_missing_wallets_section():
    with pytest.raises(ConfigError, match="non-empty list"):
        parse_watch_config({"labels": {}})


def test_labels_are_validated_and_keyed_by_chain():
    config = parse_watch_config(
        {"wallets": [{"name": "Main", "address": MAIN}], "labels": {COLD: "My Ledger"}}
    )
    assert config.labels == {("ethereum", COLD.lower()): "My Ledger"}


def test_invalid_label_address_is_rejected():
    with pytest.raises(ConfigError):
        parse_watch_config(
            {"wallets": [{"name": "Main", "address": MAIN}], "labels": {"0xnope": "Bad"}}
        )


def test_ignore_assets():
    config = parse_watch_config(
        {"wallets": [{"name": "Main", "address": MAIN}], "ignore_assets": [COLD]}
    )
    assert config.ignore_assets == frozenset({("ethereum", COLD.lower())})


def test_yaml_file_round_trip(tmp_path: Path):
    path = tmp_path / "wallets.yml"
    path.write_text(
        f"""
wallets:
  - name: Main
    chain: ethereum
    address: "{MAIN}"
  - name: Cold
    address: "{COLD}"

labels:
  "{CHECKSUMMED}": "Binance"
"""
    )
    config = load_watch_config(path)

    assert [w.name for w in config.wallets] == ["Main", "Cold"]
    assert config.labels[("ethereum", CHECKSUMMED.lower())] == "Binance"


def test_missing_file_is_a_clear_error(tmp_path: Path):
    with pytest.raises(ConfigError, match="does not exist"):
        load_watch_config(tmp_path / "nope.yml")


def test_a_directory_in_place_of_the_config_is_explained(tmp_path: Path):
    """Docker creates a directory for a bind mount whose source is missing."""
    (tmp_path / "wallets.yml").mkdir()
    with pytest.raises(ConfigError, match="is a directory"):
        load_watch_config(tmp_path / "wallets.yml")


def test_malformed_yaml_is_a_clear_error(tmp_path: Path):
    path = tmp_path / "wallets.yml"
    path.write_text("wallets: [\n  - broken")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_watch_config(path)


def test_settings_require_a_provider_key():
    with pytest.raises(ConfigError, match="ALCHEMY_API_KEY"):
        load_settings({"PATH": "/usr/bin"})


def test_settings_from_environment(tmp_path: Path):
    settings = load_settings(
        {
            "ALCHEMY_API_KEY": "secret",
            "BLOCKTAIL_CONFIG": str(tmp_path / "w.yml"),
            "BLOCKTAIL_DB": str(tmp_path / "db.sqlite"),
            "SYNC_INTERVAL_SECONDS": "30",
            "BACKFILL_DAYS": "7",
            "REORG_DEPTH": "12",
            "PORT": "9000",
        }
    )

    assert settings.alchemy_url.endswith("/secret")
    assert settings.sync_interval == 30
    assert settings.backfill_blocks == 7 * 7200
    assert settings.reorg_depth == 12
    assert settings.port == 9000
    assert settings.stale_after == 180


def test_sync_interval_defaults_to_a_free_tier_safe_value():
    settings = load_settings({"ALCHEMY_API_KEY": "secret"})
    assert settings.sync_interval == 90
    assert settings.stale_after == 270


def test_alchemy_url_overrides_the_key():
    settings = load_settings({"ALCHEMY_URL": "https://self-hosted.example/rpc"})
    assert settings.alchemy_url == "https://self-hosted.example/rpc"


# ------------------------------------------------------------- schema upgrade


def test_an_existing_database_upgrades_without_being_recreated(tmp_path: Path):
    """The whole point of migrations: a v1 file gains v2 and keeps its rows."""
    import sqlite3

    from app.db import MIGRATIONS_DIR, Database

    database_path = tmp_path / "blocktail.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL);"
        + (MIGRATIONS_DIR / "001_initial.sql").read_text()
        + "\nINSERT INTO schema_migrations (version, applied_at) VALUES (1, 0);"
        "\nINSERT INTO accounts (chain_id, address, name, position, active, created_at)"
        " VALUES ('ethereum', '0xabc', 'Main', 0, 1, 0);"
        "\nINSERT INTO sync_state (chain_id, last_synced_block, backfill_done)"
        " VALUES ('ethereum', 21000000, 1);"
    )
    connection.commit()
    connection.close()

    database = Database(database_path)
    applied = database.migrate()

    assert applied == [2, 3, 4, 5, 6], "only the pending migrations run"
    assert [a.name for a in database.list_accounts()] == ["Main"], "existing rows survive"
    assert database.prices_by_asset() == {}

    # An account that predates the ownership flag is mine, and predates the
    # per-account backfill, so it must not be re-scanned from scratch.
    existing = database.list_accounts()[0]
    assert existing.is_owned is True
    assert existing.source == "config"
    assert existing.synced_to_block == 21_000_000, (
        "an account indexed before the upgrade inherits the watermark it had,"
        " so it is not re-scanned from scratch"
    )

    assert database.migrate() == [], "re-running is a no-op"
    database.close()


def test_an_unreadable_config_explains_itself(tmp_path: Path):
    """The container runs unprivileged; a 0600 file owned by root on the host is
    unreadable inside it. This failed a real deploy, as a bare traceback."""
    import os

    path = tmp_path / "wallets.yml"
    path.write_text("wallets: []")
    os.chmod(path, 0o000)
    if os.access(path, os.R_OK):
        pytest.skip("running as root: permissions are not enforced")

    with pytest.raises(ConfigError) as info:
        load_watch_config(path)

    assert "chmod 644" in str(info.value)
    assert "unprivileged" in str(info.value)
