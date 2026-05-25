-- T1 verify — acceptanskriterier för mcp_readonly-rollen.
--
-- Körs som pgadmin (eller mcp_readonly för de delar som krävs).
-- Alla rader ska vara TRUE eller stämma med kommentaren.

\set ON_ERROR_STOP on
\pset null '(null)'

\echo ''
\echo '=== T1.A — Rollen finns med rätt inställningar ==='
SELECT rolname,
       rolcanlogin                       AS can_login,           -- t
       rolsuper                          AS is_superuser,         -- f
       rolbypassrls                      AS bypass_rls,           -- f
       rolconfig                         AS session_settings
FROM pg_roles
WHERE rolname = 'mcp_readonly';

\echo ''
\echo '=== T1.B — Rollen är INTE medlem i azure_pg_admin ==='
SELECT EXISTS (
  SELECT 1
  FROM pg_auth_members m
  JOIN pg_roles r ON r.oid = m.roleid
  JOIN pg_roles u ON u.oid = m.member
  WHERE u.rolname = 'mcp_readonly'
    AND r.rolname IN ('azure_pg_admin', 'pg_write_all_data')
) AS is_admin_member;
-- Förväntat: f

\echo ''
\echo '=== T1.C — SELECT-rättigheter på alla public-tabeller ==='
SELECT table_name,
       has_table_privilege('mcp_readonly', 'public.'||table_name, 'SELECT') AS can_select,
       has_table_privilege('mcp_readonly', 'public.'||table_name, 'INSERT') AS can_insert,
       has_table_privilege('mcp_readonly', 'public.'||table_name, 'UPDATE') AS can_update,
       has_table_privilege('mcp_readonly', 'public.'||table_name, 'DELETE') AS can_delete
FROM information_schema.tables
WHERE table_schema = 'public'
ORDER BY table_name;
-- Förväntat: can_select=t för alla; can_insert/update/delete=f för alla.

\echo ''
\echo '=== T1.D — Kan INTE skapa objekt i public ==='
SELECT has_schema_privilege('mcp_readonly', 'public', 'CREATE') AS can_create_in_public,
       has_schema_privilege('mcp_readonly', 'public', 'USAGE')  AS can_use_public;
-- Förväntat: can_create=f, can_use=t

\echo ''
\echo '=== T1.E — Database CONNECT, ingen CREATE/TEMP ==='
SELECT has_database_privilege('mcp_readonly', 'finance', 'CONNECT') AS can_connect,
       has_database_privilege('mcp_readonly', 'finance', 'CREATE')  AS can_create_db,
       has_database_privilege('mcp_readonly', 'finance', 'TEMP')    AS can_create_temp;
-- Förväntat: connect=t, create=f, temp=f (temp följer PUBLIC-default; ok om f)

\echo ''
\echo '=== T1.F — Manuell test: anslut som mcp_readonly och testa ==='
\echo 'Kör i separat session med rollens connection string:'
\echo ''
\echo '  -- Ska gå:'
\echo '  SELECT current_user, current_setting(''default_transaction_read_only'');'
\echo '  -- Förväntat: mcp_readonly | on'
\echo ''
\echo '  SELECT COUNT(*) FROM fact_balances;'
\echo '  -- Ska returnera ett tal.'
\echo ''
\echo '  -- Ska FAILA:'
\echo '  INSERT INTO fact_balances (company_id, period, period_type, account_code, amount,'
\echo '         currency, source_kind, source_file, scenario, loaded_at)'
\echo '  VALUES (999, ''209912'', ''monthly'', ''9999'', 0, ''SEK'', ''IMP'', ''test'', ''A'', now());'
\echo '  -- Förväntat fel: cannot execute INSERT in a read-only transaction'
