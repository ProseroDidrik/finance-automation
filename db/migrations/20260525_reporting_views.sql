-- T3 — PII-minimering via reporting-vyer.
--
-- mcp_readonly ser idag persondata i klartext på public.fact_personnel
-- (namn, födelsedatum, kön, lön, avslutsorsak) och journal-fritext kan
-- innehålla personnummer. Dataminimering (GDPR art. 5.1c) saknas.
--
-- Den här migrationen:
--   1. Skapar schema `reporting` med pseudonymiserade/maskerade vyer
--   2. GRANT:ar mcp_readonly SELECT på reporting.*
--   3. REVOKE:ar mcp_readonly:s direktaccess på PII-råtabellerna
--
-- KONSERVATIVA DEFAULTS — kräver DPO-beslut innan finjustering:
--   - employee_name      → pseudonym 'EMP_{id}' (surrogat istället för PII)
--   - birth_date         → birth_year (för åldersfördelning, inte exakt datum)
--   - salary_local       → BORTTAGEN ur vyn ⚠️ AWAITING_DPO
--                          (juridik avgör om lön behövs för analys)
--   - termination_reason → BORTTAGEN ur vyn ⚠️ AWAITING_DPO
--                          (frikoppling-orsak är ofta känslig)
--   - journal-fritext    → personnummer-mönster ([0-9]{6}[-+][0-9]{4})
--                          ersätts med '[PNR]'
--
-- INTE behandlat här (separat ticket / T9-iteration):
--   - dim_supplier_register.supplier_name / fact_supplier_spend.namn
--     (kan inkludera enskild firma med personnamn)
--   - dim_account_map.description (fri text, sällan PII men möjligt)
--
-- Externa MCP-testare (Eva, Erik) får 'permission denied' om de queryar
-- public.fact_personnel direkt efter den här migrationen — de måste byta
-- till reporting.personnel. Se RUNBOOK_T3.md för meddelande till dem.

\set ON_ERROR_STOP on

------------------------------------------------------------------------
-- 1) Schema reporting
------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS reporting;
GRANT USAGE ON SCHEMA reporting TO mcp_readonly;

------------------------------------------------------------------------
-- 2) reporting.personnel — pseudonymiserad personalvy
------------------------------------------------------------------------
-- CREATE OR REPLACE VIEW är idempotent och säker även om kolumnsetet
-- ändras (DROP + recreate skulle bryta beroende vyer/permissions).
CREATE OR REPLACE VIEW reporting.personnel AS
SELECT
    id,
    country,
    company_id,
    'EMP_' || id::text                 AS employee_ref,    -- pseudonym
    title,
    EXTRACT(YEAR FROM birth_date)::int AS birth_year,      -- inte exakt datum
    employed_from,
    employed_to,
    -- termination_reason                                  -- AWAITING_DPO: utesluten
    employment_pct,
    productivity,
    billable_pct,
    gender,
    category,
    -- salary_local                                        -- AWAITING_DPO: utesluten
    location,
    apprenticeship_end,
    pension_apprentice,
    snapshot_date
FROM public.fact_personnel;

COMMENT ON VIEW reporting.personnel IS
'PII-minimerad personalvy för MCP/analys. Namn pseudonymiserade (EMP_{id}), '
'födelsedatum grovkornat till år, lön och avslutsorsak utelämnade pending DPO-beslut. '
'Se db/migrations/20260525_reporting_views.sql för designval.';

------------------------------------------------------------------------
-- 3) reporting.journal_sie — fritext-fält PNR-maskade
------------------------------------------------------------------------
-- Svenskt personnummer-mönster: 6 siffror + '-' eller '+' + 4 siffror.
-- '+' används för personer 100+ år. Maskar inte andra format (samordnings-
-- nummer, organisationsnummer) — om de dyker upp behöver regex utökas.
CREATE OR REPLACE VIEW reporting.journal_sie AS
SELECT
    id,
    company_id,
    period,
    series,
    voucher_number,
    voucher_date,
    regexp_replace(COALESCE(voucher_text, ''),
                   '[0-9]{6}[-+][0-9]{4}', '[PNR]', 'g') AS voucher_text,
    line_no,
    account_code,
    account_name,
    amount,
    regexp_replace(COALESCE(transaction_text, ''),
                   '[0-9]{6}[-+][0-9]{4}', '[PNR]', 'g') AS transaction_text,
    quantity,
    currency,
    source_file,
    loaded_at
FROM public.fact_journal_sie;

COMMENT ON VIEW reporting.journal_sie IS
'SIE-verifikat med svenska personnummer-mönster maskade till [PNR] i voucher_text '
'och transaction_text. Övriga kolumner oförändrade.';

------------------------------------------------------------------------
-- 4) reporting.journal_saft — line_description PNR-maskad
------------------------------------------------------------------------
CREATE OR REPLACE VIEW reporting.journal_saft AS
SELECT
    id,
    company_id,
    period,
    journal_id,
    journal_description,
    transaction_id,
    transaction_date,
    -- transaction_description ligger på transaktionsnivå (en per voucher) —
    -- ofta bara typ "Faktura 2024-04" men maskera defensivt ändå.
    regexp_replace(COALESCE(transaction_description, ''),
                   '[0-9]{6}[-+][0-9]{4}', '[PNR]', 'g') AS transaction_description,
    line_no,
    record_id,
    account_code,
    debit_amount,
    credit_amount,
    amount,
    regexp_replace(COALESCE(line_description, ''),
                   '[0-9]{6}[-+][0-9]{4}', '[PNR]', 'g') AS line_description,
    currency,
    source_file,
    loaded_at
FROM public.fact_journal_saft;

COMMENT ON VIEW reporting.journal_saft IS
'SAF-T-verifikat med svenska personnummer-mönster maskade till [PNR] i '
'line_description och transaction_description. Övriga kolumner oförändrade.';

------------------------------------------------------------------------
-- 5) GRANT mcp_readonly SELECT på reporting.*
------------------------------------------------------------------------
GRANT SELECT ON ALL TABLES IN SCHEMA reporting TO mcp_readonly;
-- Framtida vyer i reporting.* får också SELECT automatiskt.
-- (ALTER DEFAULT PRIVILEGES gäller objekt skapade av current_user = pgadmin.)
ALTER DEFAULT PRIVILEGES IN SCHEMA reporting
  GRANT SELECT ON TABLES TO mcp_readonly;

------------------------------------------------------------------------
-- 6) REVOKE mcp_readonly:s direktaccess på PII-råtabellerna.
--    Tvingar all PII-läsning genom reporting-vyerna.
------------------------------------------------------------------------
REVOKE SELECT ON public.fact_personnel    FROM mcp_readonly;
REVOKE SELECT ON public.fact_journal_sie  FROM mcp_readonly;
REVOKE SELECT ON public.fact_journal_saft FROM mcp_readonly;

------------------------------------------------------------------------
-- 7) Säkerställ att etl_writer fortsatt kan skriva till PII-tabellerna.
--    REVOKE ovan rörde bara mcp_readonly, men sanity-check explicit.
--    (Verifyfilen kollar det här explicit också.)
------------------------------------------------------------------------
-- Inget att göra — etl_writer fick INSERT/UPDATE/DELETE i T2 och de är
-- separata grants från mcp_readonly:s SELECT.

\echo ''
\echo '[OK] T3 — reporting-vyer skapade, mcp_readonly:s PII-access borttagen.'
\echo '     Meddela MCP-testare: byt public.fact_personnel → reporting.personnel.'
