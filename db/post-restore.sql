-- Re-apply database-level settings a single-database pg_restore cannot carry.
--
-- pg_dump of one database does not include GRANT/REVOKE ON DATABASE, so a
-- restored copy silently loses migration 0017's lockdown (PUBLIC may connect
-- and create temp tables again). Run after every restore; safe to repeat.
DO $$
BEGIN
    EXECUTE format('REVOKE CONNECT, TEMPORARY ON DATABASE %I FROM PUBLIC', current_database());
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'superset_ro') THEN
        EXECUTE format('GRANT CONNECT ON DATABASE %I TO superset_ro', current_database());
    END IF;
END $$;
