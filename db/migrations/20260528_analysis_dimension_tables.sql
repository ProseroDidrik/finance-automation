-- T-dimensions: grants för analys-dimensionstabellerna.
-- Tabellerna skapas i db.py SCHEMA_SQL; denna migration sätter bara rättigheter.
-- mcp_readonly: SELECT (ingen PII i dessa tabeller). etl_writer: SELECT/INSERT/
-- DELETE (DML, ingen DDL) + sekvens-USAGE för fact_saft_analysis.id.
-- Idempotent: GRANT är idempotent i Postgres.

GRANT SELECT ON dim_analysis_type, dim_analysis_member, fact_saft_analysis
    TO mcp_readonly;

GRANT SELECT, INSERT, DELETE ON dim_analysis_type, dim_analysis_member,
    fact_saft_analysis TO etl_writer;

GRANT USAGE, SELECT ON SEQUENCE seq_fact_saft_analysis TO etl_writer;
