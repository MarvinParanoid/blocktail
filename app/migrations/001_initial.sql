-- Core schema. Column names are chain-neutral on purpose: `chain_id`, `address`,
-- `balance` rather than `ethereum_address` / `eth_balance`, so that adding a
-- second chain is an INSERT, not an ALTER TABLE across every table.

CREATE TABLE accounts (
    id           INTEGER PRIMARY KEY,
    chain_id     TEXT    NOT NULL,
    address      TEXT    NOT NULL,
    name         TEXT    NOT NULL,
    position     INTEGER NOT NULL DEFAULT 0,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   INTEGER NOT NULL,
    UNIQUE (chain_id, address)
);

CREATE TABLE assets (
    id               INTEGER PRIMARY KEY,
    chain_id         TEXT    NOT NULL,
    -- '' identifies the chain's native asset; a token stores its contract.
    contract_address TEXT    NOT NULL,
    symbol           TEXT    NOT NULL,
    decimals         INTEGER NOT NULL,
    UNIQUE (chain_id, contract_address)
);

-- One row per on-chain value movement. A transaction that moves value several
-- times produces several rows, distinguished by `event_key`.
CREATE TABLE activities (
    id              INTEGER PRIMARY KEY,
    chain_id        TEXT    NOT NULL,
    event_key       TEXT    NOT NULL,
    tx_hash         TEXT    NOT NULL,
    block_number    INTEGER NOT NULL,
    block_timestamp INTEGER NOT NULL,
    kind            TEXT    NOT NULL,
    asset_id        INTEGER NOT NULL REFERENCES assets (id),
    amount_raw      TEXT    NOT NULL,
    from_address    TEXT    NOT NULL,
    to_address      TEXT    NOT NULL,
    -- Set when the counterparty on that side is a monitored account. Both set
    -- means a transfer between two monitored wallets: one activity, not two.
    from_account_id INTEGER REFERENCES accounts (id),
    to_account_id   INTEGER REFERENCES accounts (id),
    UNIQUE (chain_id, event_key)
);

CREATE INDEX idx_activities_feed      ON activities (block_timestamp DESC, id DESC);
CREATE INDEX idx_activities_from_acct ON activities (from_account_id, block_timestamp DESC, id DESC);
CREATE INDEX idx_activities_to_acct   ON activities (to_account_id, block_timestamp DESC, id DESC);
CREATE INDEX idx_activities_asset     ON activities (asset_id, block_timestamp DESC, id DESC);
CREATE INDEX idx_activities_tx        ON activities (tx_hash);
CREATE INDEX idx_activities_block     ON activities (chain_id, block_number);

CREATE TABLE balances (
    account_id INTEGER NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    asset_id   INTEGER NOT NULL REFERENCES assets (id),
    amount_raw TEXT    NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (account_id, asset_id)
);

-- User-supplied names for arbitrary external addresses.
CREATE TABLE labels (
    chain_id TEXT NOT NULL,
    address  TEXT NOT NULL,
    label    TEXT NOT NULL,
    PRIMARY KEY (chain_id, address)
);

CREATE TABLE sync_state (
    chain_id          TEXT PRIMARY KEY,
    last_synced_block INTEGER,
    head_block        INTEGER,
    last_success_at   INTEGER,
    last_attempt_at   INTEGER,
    last_error        TEXT,
    backfill_done     INTEGER NOT NULL DEFAULT 0
);
