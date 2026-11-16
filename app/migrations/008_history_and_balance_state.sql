-- Two things the indexer reports separately from "did the last cycle index".
--
-- A request for older history ("Load N more days") is work the page asks for
-- and the background indexer does. Its state outlives the request and the
-- process: queued, running, completed or failed, the block it reaches for,
-- and why it failed.
ALTER TABLE sync_state ADD COLUMN history_status       TEXT;
ALTER TABLE sync_state ADD COLUMN history_target       INTEGER;
ALTER TABLE sync_state ADD COLUMN history_requested_at INTEGER;
ALTER TABLE sync_state ADD COLUMN history_finished_at  INTEGER;
ALTER TABLE sync_state ADD COLUMN history_error        TEXT;

-- Balances are read after the transfers are stored. Their failure used to be
-- the cycle's failure, which held back the watermark of everything that had
-- been indexed fine; now it is recorded on its own.
ALTER TABLE sync_state ADD COLUMN balances_error     TEXT;
ALTER TABLE sync_state ADD COLUMN balances_failed_at INTEGER;
ALTER TABLE sync_state ADD COLUMN balances_ok_at     INTEGER;
