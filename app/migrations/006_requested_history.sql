-- How far back the reader has asked to see.
--
-- BACKFILL_DAYS is a deployment default; this is the running instance being
-- told "further, please" and remembering it across restarts. Without it, asking
-- for older history would work until the next `docker compose up` and then
-- silently undo itself.
ALTER TABLE sync_state ADD COLUMN requested_from_block INTEGER;
