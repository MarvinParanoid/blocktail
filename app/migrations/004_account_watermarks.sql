-- Per-account watermark, replacing the `backfilled` flag.
--
-- A boolean could only say "has this account ever been scanned". It could not
-- say *how far*, which matters the moment an account can be archived: while it
-- is inactive the indexer skips it, so its history gains a gap. Re-adding it
-- should resume from where it stopped — not re-scan everything (expensive) and
-- not jump to the head (leaving the gap).
ALTER TABLE accounts ADD COLUMN synced_to_block INTEGER;

UPDATE accounts
SET synced_to_block = (
    SELECT last_synced_block FROM sync_state WHERE sync_state.chain_id = accounts.chain_id
)
WHERE backfilled = 1;

ALTER TABLE accounts DROP COLUMN backfilled;
