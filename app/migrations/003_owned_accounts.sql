-- Watching an address and owning it are different things. An account that is
-- not owned still gets indexed, still shows its balances and still appears in
-- the feed — it just never counts towards "how much do I have".
ALTER TABLE accounts ADD COLUMN is_owned INTEGER NOT NULL DEFAULT 1;

-- Where the account came from. Config-declared accounts are re-applied from the
-- file on every start; accounts added through the UI live only here and must
-- survive that.
ALTER TABLE accounts ADD COLUMN source TEXT NOT NULL DEFAULT 'config';

-- Whether this account's history has been fetched. An account added after the
-- first sync starts unbackfilled, so the indexer reaches back for it instead of
-- only picking it up from the current head.
ALTER TABLE accounts ADD COLUMN backfilled INTEGER NOT NULL DEFAULT 0;

-- Accounts that already exist were indexed under the previous, global backfill,
-- so they are done. Only rows inserted from here on start unbackfilled.
UPDATE accounts SET backfilled = 1;

CREATE INDEX idx_accounts_owned ON accounts (is_owned, active);
