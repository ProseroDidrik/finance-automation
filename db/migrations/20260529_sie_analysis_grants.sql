-- SIE-dimensioner: grants för analys-dimensionstabellen.
-- Tabellen skapas i db.py SCHEMA_SQL; denna migration sätter bara rättigheter.
-- mcp_readonly: SELECT (ingen PII i denna tabell). etl_writer: SELECT/INSERT/
-- DELETE (DML, ingen DDL) + sekvens-USAGE för fact_sie_analysis.id.
-- Idempotent: GRANT är idempotent i Postgres.

GRANT SELECT ON fact_sie_analysis TO mcp_readonly;

GRANT SELECT, INSERT, DELETE ON fact_sie_analysis TO etl_writer;

GRANT USAGE, SELECT ON SEQUENCE seq_fact_sie_analysis TO etl_writer;
