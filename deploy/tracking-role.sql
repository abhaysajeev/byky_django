-- The RFID gateway's own database role (design/07-tracking.md §2).
--
-- Run ONCE per environment, by hand, as the database owner, AFTER the
-- tracking migrations have run, e.g.:
--
--   docker exec -i <project>-db-1 psql -U <owner> -d <database> \
--     -v pw="'<a new strong password>'" < deploy/tracking-role.sql
--
-- then put the same password in .env:
--   TRACKING_DB_URL=postgresql://tracking_writer:<password>@db:5432/<database>
--
-- The role can write only the gateway's tracking tables, holds at most 5
-- connections, and any statement over 5 s is cancelled -- so a misbehaving
-- gateway cannot lock or load the business tables.

CREATE ROLE tracking_writer LOGIN PASSWORD :pw CONNECTION LIMIT 5;
ALTER ROLE tracking_writer SET statement_timeout = '5s';
ALTER ROLE tracking_writer SET idle_in_transaction_session_timeout = '30s';

GRANT SELECT, INSERT ON tracking_reading, tracking_reading_key, tracking_raw_frame TO tracking_writer;
GRANT SELECT, INSERT, UPDATE ON tracking_tag_last_seen TO tracking_writer;
GRANT SELECT, INSERT, UPDATE ON tracking_reader TO tracking_writer;
GRANT SELECT ON tracking_antenna, tracking_reader_assignment TO tracking_writer;
GRANT USAGE ON SEQUENCE tracking_reader_id_seq, tracking_raw_frame_id_seq TO tracking_writer;
