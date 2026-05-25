-- T9-followup — höj mcp_readonly statement_timeout från 30s → 60s.
--
-- Bakgrund: T9 pekade webapp till mcp_readonly. Webapps compare_coverage-endpoint
-- queryar fact_journal_sie/saft (10M+ rader) för att summera per (bolag, period,
-- konto). Default 4-månaders range tar ~29s första körningen (cache miss på SAFT
-- dedup-sort). T1:s 30s timeout är för snäv → QueryCanceled.
--
-- 60s ger marginal för webapp:s tunga aggregat utan att öppna upp för verkliga
-- DoS-attacker. MCP-användare kan dra på sig en längre paus om de skickar tunga
-- queries men det är acceptabelt — Claude.ai-konversationer toleras 60s spinner.
--
-- Idempotent. Kan köras om vid behov.

\set ON_ERROR_STOP on

ALTER ROLE mcp_readonly SET statement_timeout = '60s';

\echo ''
\echo '[OK] mcp_readonly statement_timeout = 60s (från 30s).'
\echo '     Påverkar både MCP-anrop och webapp/api/compare/coverage.'
