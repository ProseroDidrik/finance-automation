# pg_stat_statements-granskning 2026-05-25

**Syfte:** Beslutsunderlag för Fas 1 (perf-optimering) — Task 10 Steg 6 enligt
`docs/prestanda-optimering-fas0-plan.md`.

**Period:** Stats samlade sedan `stats_reset = 2026-05-08 07:50 UTC` → 17 dagar live-trafik.

**Slutsats:** **Stanna vid Fas 0.** Ingen prod-query motiverar ytterligare compute
(~+39 USD/mån). Top-tider domineras av ad-hoc admin-arbete och en känd
ocachad endpoint som redan har "fix på hyllan".

---

## Fördelning per roll (17 dagar)

| Roll | Total tid | Anrop | % av DB-tid | Vad |
|---|---:|---:|---:|---|
| `pgadmin` | 3063 s | 2,229,168 | 95.3% | ETL-laddningar (INSERT/DELETE-batches) + ad-hoc admin-queryer |
| `mcp_readonly` | 150 s | 221 | 4.7% | **All MCP + webapp-trafik** (post-T1/T9) |
| `azuresu` | 0 s | 603 | 0.0% | Azure internal mgmt |
| `etl_writer` | 0 s | 21 | 0.0% | Knappt använt (rollen live från 2026-05-25; tidigare data var pgadmin) |

**Insikt:** MCP+webapp = 4.7% av DB-tiden. Resten är ETL + admin. Fas 1 (mer
compute) skulle främst gynna ETL, inte slutanvändar-MCP/webapp.

## Top 10 efter total exekveringstid (alla roller)

| # | Roll | Cum ms | Calls | Mean ms | Vad |
|---|---|---:|---:|---:|---|
| 1 | pgadmin | 576,897 | 13 | 44,377 | Täckningsmatris-query (Mercur-facit vs laddad data) — sett från admin (compare_coverage.sql) |
| 2 | pgadmin | 497,056 | 1 | 497,056 | **Min egen** check_saft_journal_dups från idag (ad-hoc) |
| 3 | pgadmin | 346,140 | 5 | 69,228 | `COUNT(*) FROM fact_journal_saft` (ad-hoc inspection) |
| 4 | pgadmin | 318,638 | 1 | 318,638 | **Min egen** GROUP BY dup-count från idag (ad-hoc) |
| 5 | pgadmin | 304,528 | 2,139,575 | 0.1 | INSERT batch fact_journal_sie — mikrokostnad, normalt |
| 6 | pgadmin | 221,740 | 4 | 55,435 | `COUNT(*) FROM fact_journal_sie` (ad-hoc) |
| 7 | pgadmin | 201,449 | 104 | 1,937 | DELETE fact_journal_sie (override-cleanup vid laddning) |
| 8 | pgadmin | 186,952 | 74 | 2,526 | P&L-aggregation (report_pnl.sql) — admin-anrop, lokala dev-tester |
| 9 | pgadmin | 138,512 | 2 | 69,256 | `COUNT(*) reporting.journal_sie WHERE voucher_text ~ ...` (PNR-regex-test) |
| 10 | mcp_readonly | 74,385 | 3 | 24,795 | **Täckningsmatris via MCP/webapp** — `/api/compare/coverage` |

**Filtrering till bara "äkta prod-trafik" (mcp_readonly):**

| Calls | Cum ms | Mean ms | Query |
|---:|---:|---:|---|
| 3 | 74,385 | 24,795 | Täckningsmatris (compare_coverage.sql) |
| 1 | 29,111 | 29,111 | Sample-stats aggregation (engångs-experiment) |
| 1 | 27,040 | 27,040 | `COUNT(*) reporting.journal_sie` (engångs-test) |
| 1 | 15,498 | 15,498 | `COUNT(*) reporting.journal_saft` (engångs-test) |
| 1 | 2,178 | 2,178 | `COUNT(*) fact_balances` (engångs) |
| 191 | 98 | 0.5 | `SELECT $1` (psycopg parameter-prepare, ignoreras) |

## Tolkning

**Top "äkta" repeated query:** Täckningsmatrisen (3 anrop, 25s/anrop = 74s totalt
för mcp_readonly + 13 admin-anrop = 650s totalt). Detta är `/api/compare/coverage`
i webappen — sammanstämmer perfekt med `project_perf_optimization`-memorys
30s/anrop-observation.

**Ingenting annat repeaterad:** Alla andra mcp_readonly-toppar är engångskörningar
(experiment, ad-hoc-tester).

**ETL-mikrokostnader är OK:** 304s totalt över 2.1M INSERT-anrop = 0.14ms/row.
Bottleneck'en där är inte query-tid utan IOPS (120 IOPS B1ms).

## Beslut

**Fas 1 = SKIPPED.** Skäl:
1. MCP+webapp-trafiken är 4.7% av DB-tiden — inte bottleneck.
2. Topen är ETL (95.3%) som inte gynnas av Fas 1's compute-bump i samma utsträckning.
3. Täckningsmatrisens 30s är dokumenterat "acceptabelt för sällan-använd avstämning".
4. "Gratis fix på hyllan" finns (cacha per period-intervall, ~10 rader) — kan
   implementeras om/när det blir irriterande, utan att uppgradera SKU.

## Follow-ups (frivilliga, ej blockerande)

- **Täckningsmatris-cache** (10 rader i `webapp/backend/main.py`) — vänder
  30s → direkt för upprepade visningar.
- **Reset pg_stat_statements** efter T1-T9-cykeln — ad-hoc-arbetet idag (mina
  egna check-queryer) skapar brus i top-listan.
  ```sql
  SELECT pg_stat_statements_reset();
  ```
- **etl_writer-stats samlas in framöver** — först nu är rollen live, så
  steady-state ETL-data dyker upp i nästa granskning.

## Re-granskning

Rekommenderas om något av följande inträffar:
- mcp_readonly:s andel > 25% av DB-tid
- Snittlatens för täckningsmatris > 60s
- Användarrapporter om långsamhet i någon webapp-vy
- Inför skarpt produktionsbruk med fler analytiker
