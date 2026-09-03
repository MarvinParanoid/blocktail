-- How far *back* each account has been scanned.
--
-- Without this, raising BACKFILL_DAYS did nothing for an account that had
-- already synced: the watermark only ever moved forward, so the indexer kept
-- resuming from the head and never reached further into the past. Asking for
-- more history quietly changed nothing.
ALTER TABLE accounts ADD COLUMN indexed_from_block INTEGER;

-- Existing accounts: the earliest block we actually hold something from, which
-- is the closest thing to a floor that can be reconstructed. An account with no
-- activity gets the chain watermark, so it simply re-scans once — which is
-- idempotent and cheap when there is nothing to find.
UPDATE accounts SET indexed_from_block = COALESCE(
    (
        SELECT MIN(act.block_number) FROM activities act
        WHERE act.from_account_id = accounts.id OR act.to_account_id = accounts.id
    ),
    (SELECT last_synced_block FROM sync_state WHERE sync_state.chain_id = accounts.chain_id)
);
