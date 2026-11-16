-- A token's decimals can be unknown. A metadata lookup that failed used to be
-- stored as decimals = 0, which reads a raw amount as whole tokens: 1 WBTC
-- (10^8 base units) became 100,000,000 WBTC. The column stays NOT NULL so the
-- table need not be rebuilt; this flag says whether its value is believed.
ALTER TABLE assets ADD COLUMN decimals_known INTEGER NOT NULL DEFAULT 1;

-- A zero written by an older version is indistinguishable from a failed
-- lookup, so it is re-learned rather than trusted: the next metadata read, or
-- the vendored token list at startup, settles it. A real zero-decimals token
-- simply gets confirmed again.
UPDATE assets SET decimals_known = 0 WHERE contract_address <> '' AND decimals = 0;
