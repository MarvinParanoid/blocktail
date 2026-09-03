-- Spot valuation for assets. Kept separate from `assets` because a price is a
-- fact about a moment, not about the asset, and because a missing row must be
-- distinguishable from a price of zero.
CREATE TABLE asset_prices (
    asset_id   INTEGER PRIMARY KEY REFERENCES assets (id) ON DELETE CASCADE,
    currency   TEXT    NOT NULL,
    price      TEXT    NOT NULL,   -- decimal string; never a float
    updated_at INTEGER NOT NULL
);
