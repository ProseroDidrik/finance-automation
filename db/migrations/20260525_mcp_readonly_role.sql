-- T1 — Dedikerad read-only-roll för warehouse-MCP:n.
--
-- Idag ansluter MCP:n som `pgadmin` (medlem i `azure_pg_admin`,
-- `rolbypassrls = true`). Enda skrivskyddet är ett regex-filter i
-- `query_sql`. Den här migrationen skapar `mcp_readonly` så DB:n SJÄLV
-- tvingar read-only, även om filtret kringgås.
--
-- Körs som `pgadmin`. Idempotent — kan köras flera gånger.
-- Lösenordet skickas via psql-variabel, inte i filen:
--   psql "$DATABASE_URL_ADMIN" -v mcp_pw="<lösen>" -f <denna fil>

\set ON_ERROR_STOP on

------------------------------------------------------------------------
-- 1) Skapa rollen om den inte finns. Utan lösenord — sätts i steg 2.
------------------------------------------------------------------------
-- psql-variabelsubstitution inuti DO-block är opålitlig (dollar-quoting
-- krockar med psql-variabel-syntax i vissa versioner). Splittra istället:
-- DO-blocket skapar strukturen, en separat ALTER-sats sätter värdet.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mcp_readonly') THEN
    CREATE ROLE mcp_readonly LOGIN;
    RAISE NOTICE 'Created role mcp_readonly';
  ELSE
    RAISE NOTICE 'Role mcp_readonly already exists — leaving definition unchanged';
  END IF;
END
$$;

------------------------------------------------------------------------
-- 2) Sätt/uppdatera lösenordet från psql-variabel.
--    Sätts varje körning så migrationen kan användas vid rotation.
------------------------------------------------------------------------
ALTER ROLE mcp_readonly WITH PASSWORD :'mcp_pw';

------------------------------------------------------------------------
-- 3) Defense-in-depth: tvinga read-only-transaktioner och kort timeout.
--    `default_transaction_read_only = on` blockerar all skrivning på
--    transaktionsnivå även om någon skulle GRANT:a INSERT senare.
--    `statement_timeout = 30s` matchar MCP-serverns timeout — en
--    skenande query kan inte hänga DB:n eller användas för DoS.
------------------------------------------------------------------------
ALTER ROLE mcp_readonly SET default_transaction_read_only = on;
ALTER ROLE mcp_readonly SET statement_timeout = '30s';

------------------------------------------------------------------------
-- 4) Rättigheter — endast SELECT, endast på public.
------------------------------------------------------------------------
GRANT CONNECT ON DATABASE finance TO mcp_readonly;
GRANT USAGE ON SCHEMA public TO mcp_readonly;

-- Befintliga tabeller
GRANT SELECT ON ALL TABLES IN SCHEMA public TO mcp_readonly;

-- Inga sequence-rättigheter: en SELECT-only-roll behöver dem inte.
-- nextval/currval används bara vid INSERT (DEFAULT nextval('seq_…')), och
-- default_transaction_read_only=on blockerar all skrivning ändå.

-- Framtida tabeller får också bara SELECT automatiskt.
-- VIKTIGT: DEFAULT PRIVILEGES gäller bara objekt skapade av rollen som
-- körde ALTER DEFAULT PRIVILEGES (default = current_user). Eftersom alla
-- nya tabeller skapas av admin-rollen är detta korrekt scope.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO mcp_readonly;

------------------------------------------------------------------------
-- 5) Säkerställ att rollen INTE kan skapa objekt i public.
--    REVOKE FROM PUBLIC är PG15+-default men explicit här så vi inte
--    förlitar oss på servernivå-default som kan ändras.
------------------------------------------------------------------------
REVOKE CREATE ON SCHEMA public FROM mcp_readonly;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

------------------------------------------------------------------------
-- Klart. Verifiera med 20260525_mcp_readonly_role.verify.sql.
------------------------------------------------------------------------
\echo ''
\echo '[OK] T1 — mcp_readonly migration applied.'
\echo '     Next: peka MCP-servern på database-url-readonly (se RUNBOOK_T1.md).'
