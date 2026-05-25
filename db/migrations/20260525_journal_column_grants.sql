-- T3.c — Column-level grants på journal-tabellerna för mcp_readonly.
--
-- Bakgrund: T3 drog in mcp_readonly:s SELECT på fact_journal_sie/saft och
-- pekade analyser till reporting.journal_*-vyerna (regex-maskar PNR i fritext).
-- För aggregat-queries (SUM(amount), COUNT) blev det ~4x långsammare eftersom
-- regexp_replace evalueras per rad även när fritextfälten inte behövs i
-- SELECT-listan — webapp:s compare_coverage timear ut på 30s mot 10M+ SIE-rader.
--
-- Lösning: column-level grants. mcp_readonly får SELECT på ALLA kolumner UTOM
-- de PII-känsliga fritextfälten:
--   fact_journal_sie:  voucher_text, transaction_text  (PNR kan dölja sig)
--   fact_journal_saft: line_description, transaction_description
--
-- Då kan webapp/MCP queryra account_code/amount/period direkt mot public-tabellen
-- (snabb access), medan SELECT * eller SELECT voucher_text fortfarande failar
-- med 'permission denied for column'. PII-minimering bevarad.
--
-- reporting.journal_sie/saft-vyerna lämnas kvar — analyser som faktiskt
-- vill se maskad fritext fortsätter använda dem.
--
-- Körs som pgadmin. Idempotent (GRANT är additivt och repeterbart).

\set ON_ERROR_STOP on

GRANT SELECT (
    id, company_id, period, series,
    voucher_number, voucher_date,
    line_no, account_code, account_name,
    amount, quantity, currency,
    source_file, loaded_at
) ON public.fact_journal_sie TO mcp_readonly;

GRANT SELECT (
    id, company_id, period,
    journal_id, journal_description,
    transaction_id, transaction_date,
    line_no, record_id, account_code,
    debit_amount, credit_amount, amount,
    currency, source_file, loaded_at
) ON public.fact_journal_saft TO mcp_readonly;

-- VIKTIGT: ingen GRANT på public.fact_personnel. Personnel-data är striktare
-- — det räcker inte att skydda fritextfält. Hela tabellen står ej till
-- mcp_readonly:s förfogande, all access måste gå via reporting.personnel.

\echo ''
\echo '[OK] T3.c — column-level grants applied. mcp_readonly kan SELECT-a allt'
\echo '     UTOM voucher_text/transaction_text/line_description.'
