-- T2 — Dedikerad skrivroll för ETL-loaders (load_*.py + delete_db.py).
--
-- Idag kör loaders som `pgadmin` (azure_pg_admin, rolbypassrls=true).
-- En bugg eller felkörning kan TRUNCATE/DELETE vad som helst, och
-- credential är adminnivå. Den här migrationen skapar `etl_writer`
-- med DML men INGEN DDL — schemaändringar reserveras för admin.
--
-- Körs som `pgadmin`. Idempotent. Lösenord via psql-variabel:
--   _apply.py 20260525_etl_writer_role.sql --var etl_pw=<lösen>

\set ON_ERROR_STOP on

------------------------------------------------------------------------
-- 1) Skapa rollen om saknas (samma mönster som T1).
------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'etl_writer') THEN
    CREATE ROLE etl_writer LOGIN;
    RAISE NOTICE 'Created role etl_writer';
  ELSE
    RAISE NOTICE 'Role etl_writer already exists — leaving definition unchanged';
  END IF;
END
$$;

------------------------------------------------------------------------
-- 2) Sätt/uppdatera lösenord.
------------------------------------------------------------------------
ALTER ROLE etl_writer WITH PASSWORD :'etl_pw';

------------------------------------------------------------------------
-- 3) Session-defaults.
--    INGEN default_transaction_read_only — vi vill skriva.
--    statement_timeout = 10 min — load_sie för stora bolag (multi-MB
--    journaler) kan ta minuter. 10 min skyddar mot skenande queries
--    utan att bryta normala bulk-inserts.
------------------------------------------------------------------------
ALTER ROLE etl_writer SET statement_timeout = '600s';

------------------------------------------------------------------------
-- 4) Rättigheter — DML på alla tabeller, USAGE/SELECT på sequences
--    (för DEFAULT nextval('seq_…') i INSERTs). INGEN DDL.
------------------------------------------------------------------------
GRANT CONNECT ON DATABASE finance TO etl_writer;
GRANT USAGE ON SCHEMA public TO etl_writer;

-- DML på datatabellerna. TRUNCATE behövs för load_account_map,
-- dim_supplier_register och liknande "rensa-och-fyll"-tabeller.
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE
  ON ALL TABLES IN SCHEMA public TO etl_writer;

-- Sequences: nextval/currval för DEFAULT nextval('seq_fact_balances') osv.
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO etl_writer;

-- Framtida tabeller/sequences får samma rättigheter automatiskt.
-- VIKTIGT: scope = objekt skapade av rollen som körde ALTER DEFAULT
-- PRIVILEGES (current_user). Eftersom alla nya tabeller skapas av admin
-- (db.py:init_schema kör som DATABASE_URL_ADMIN) är detta korrekt.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON TABLES TO etl_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO etl_writer;

------------------------------------------------------------------------
-- 5) Explicit INGEN CREATE — DDL reserveras för admin.
------------------------------------------------------------------------
REVOKE CREATE ON SCHEMA public FROM etl_writer;
-- (FROM PUBLIC sattes redan i T1.)

\echo ''
\echo '[OK] T2 — etl_writer migration applied.'
\echo '     Next: lägg URL i KV som database-url-etl + lokala DATABASE_URL_ETL.'
