# YTD-nyckeltalsdashboard (`dashboards/ytd_nyckeltal/`)

Commit:bar Python-modul som bygger Prosero-koncernens YTD-nyckeltalsdashboard
(HTML + Excel) ur finance-warehouse. Ersätter Cowork/Claude Desktop-iterationerna —
Eva-rebuild = ett kommando.

## Köra

```powershell
# Sätt DATABASE_URL (read-only räcker — mcp_readonly funkar; ingen journal läses):
$env:DATABASE_URL = (az keyvault secret show --vault-name kv-finauto-6427 `
                       --name database-url-readonly --query value -o tsv)

py dashboards/ytd_nyckeltal/build.py --period 202604 `
    --facit-dir "C:\...\Get testfiles\mercur_facit" `
    --output .\tmp\v14\
```

Flaggor:
- `--period YYYYMM` (default `202604`) — YTD-perioden i år. Jämförs mot samma månad
  fg år (`202504`) och helår fg år (`202512`, proxy för full-year-only-bolag).
- `--facit-dir` — mapp med Mercur `Resultaträkning (20).xlsx`. Utelämna eller
  `--no-validate` → ingen Mercur-jämförelse (HTML/Excel byggs ändå, utan facit-dots).
- `--output` — målmapp. Skapar `Nyckeltal.html`, `Nyckeltal.xlsx` +
  `dashboard_data.json` / `validation.json` (felsöknings-artefakter).
- `--data-only` — bygg + verifiera datalagret, skriv `dashboard_data.json`, rendera inget.

Output: `Nyckeltal.html` (~450 KB, 2 flikar: Nyckeltal + Validering, facit-dots,
drilldown) och `Nyckeltal.xlsx` (5 flikar: Sammanfattning, Nyckeltal per bolag,
Validering 2026, Validering 2025, Metod).

## Flöde

`build.py`: `db_io` (queries) → `aggregate.build_dashboard_data` → **grind: koncern
Total Sales 202604 ≈ 1591 MSEK** → `validate` (Mercur-diff + `attach_facit_to_dash`)
→ `render_html` + `render_xlsx`. Datalager-grinden körs INNAN rendering så
query/FX/best_source-buggar isoleras från renderar-buggar.

## Filer

| Fil | Roll | Ursprung |
|-----|------|----------|
| `build.py` | CLI-orkestrering | nytt |
| `config.py` | FX-kurser, top_group-listor, period-härledning, koncern-ankare | nytt |
| `db_io.py` | DB-åtkomst via repots `db.py` (read-only), json_agg-runner | nytt |
| `queries.py` | SQL-mallar (YTD_TOPGROUP, FULL_YEAR_ONLY_DETECT, PERSONNEL, DIM_COMPANY) | kopia av `skills/fte-ytd/scripts/sql_queries.py` |
| `aggregate.py` | `build_dashboard_data` — RU-aggregat + proxy-flaggning | kopia av `build_ru_aggregat.py` |
| `mercur.py` | parsa Mercur Resultaträkning-xlsx (2026 + 2025) | `parse_mercur.py` + split_col-fix |
| `validate.py` | Mercur-mappning + diff per RU (2026 + 2025) + attach | `validate_facit.py` + 2025-stöd |
| `render_html.py` | template-baserad HTML-generering | ersätter `update_html.py` |
| `render_xlsx.py` | Excel-bygge | `build_xlsx.py` + buggfixar |
| `templates/dashboard_base.html` | HTML-skelett (CSS+JS, data tokeniserad) | extraherad ur v13-HTML |

## Underhåll

**Uppdatera Mercur-facit (månadsvis):** Eva lägger nya `Resultaträkning (20/21).xlsx`
i `mercur_facit/`. Inget kodbyte — kör `build.py` med rätt `--facit-dir`.

**Lägga till nytt bolag:** registrera i `dim_company` (via `push_master.py` + `py db.py`)
och, om Mercur-namnet skiljer sig från warehouse-namnet, lägg en rad i
`validate.MERCUR_TO_CID`.

**Bygga om HTML-templaten** (om Cowork levererar ny design):
`render_html.extract_template(ny_html, "templates/dashboard_base.html")`.

**FX-kurser:** hårdkodade i `config.FX` (månadssnitt mot SEK). Dynamisk hämtning ur
`dim_exchange_rate` är en follow-up.

## Pitfalls (respektera)

- `fact_balances.amount` är YTD för SE/NO, monthly för FI/DK/DE — `queries.py`
  hanterar det via best_source + period_type. Summera aldrig rått över länder.
- Teckenkonvention: SIE (intäkt negativ). `aggregate` använder `abs()` per
  (cid, period, top_group).
- **SAFT_VER:** NO-bolag med bara helårs-SAF-T får YTD 2025 syntetiserad ur journalen
  (`synthesize_saft_ver.py`). Kvar som helår-proxy (🔸) är bolag vars journal inte
  når tillbaka (t.ex. cid 233 Stavanger — journal bara december). Det är förväntat,
  inte ett fel.
- **Återinför INTE** CENTR-valuta-override i koden — `dim_company.currency` är rättad
  i prod och läses rakt.

## Tester

```powershell
py -m pytest dashboards/ytd_nyckeltal/tests/
```

`test_aggregate.py` (RU-bygge + proxy-flaggning), `test_validate.py`
(MERCUR_TO_CID + RU-mappning + 2025-attach). Hermetiska — ingen DB.

## Uppskjutet (separat PR)

- **Aaro-klassificering** — flik + `AARO_DATA` i HTML. Läggs i
  `dashboards/ytd_nyckeltal/aaro/`. v1 injicerar `AARO_DATA = []`.
- Scheduler (cron/Task Scheduler) — v1 är bara CLI.
- Dynamiska FX ur `dim_exchange_rate`.
