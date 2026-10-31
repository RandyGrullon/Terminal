-- Row level security pass, run by app.migrations after the baseline and the migrations at EVERY start (PostgreSQL).
-- Defense in depth on Supabase: RLS on (no policies => anon/authenticated roles see nothing); the backend connects as
-- the table owner. Only tables whose RLS is still off are altered: ALTER TABLE ... ENABLE ROW LEVEL SECURITY takes an
-- ACCESS EXCLUSIVE lock, and re-running it on every start would take that lock on every table while a standby
-- initialises the schema next to the active instance during a deploy overlap -- brief blocking, with a chance of a
-- deadlock abort. Filtering on pg_class.relrowsecurity keeps the block idempotent and lock-free once RLS is set, and
-- covers the tables that migrations create.
DO $$
DECLARE t record;
BEGIN
  FOR t IN
    SELECT c.relname
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = '{schema}' AND c.relkind = 'r' AND NOT c.relrowsecurity
  LOOP
    EXECUTE format('ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY', '{schema}', t.relname);
  END LOOP;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    EXECUTE 'REVOKE ALL ON SCHEMA {schema} FROM anon';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    EXECUTE 'REVOKE ALL ON SCHEMA {schema} FROM authenticated';
  END IF;
END $$;
