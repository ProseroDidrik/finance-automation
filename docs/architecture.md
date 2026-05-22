# Arkitekturgenomgång — finance-automation

**Status:** Nulägesbeskrivning per 2026-05-22. Repot är 23 dagar gammalt
(första commit 2026-04-29), 143 commits, ~12 500 rader Python i ~38 platta
rotfiler plus `scripts/`, `webapp/` och `docs/`.

Detta dokument beskriver *hur systemet är byggt* och *flaggar observationer*.
Det innehåller medvetet inga rekommendationer om ändringar.

---

## 1. Systemöversikt

Fyra delsystem som delar ett gemensamt datalager (Azure Postgres,
`finance-warehouse`):

| Delsystem | Vad | Var |
|-----------|-----|-----|
| **ETL-pipeline** | extract → process → load av nordisk redovisningsdata | rotfiler `*.py` |
| **MCP-server** | exponerar warehouse read-only mot Claude | `mcp_server.py` |
| **Webapp** | FastAPI-backend + React/TS-frontend, rapporterings-GUI | `webapp/` |
| **Lokal kontrollpanel** | PySide6-GUI som driver/övervakar ETL-scripten | `gui*.py` |

ETL-pipelinen *skriver* till warehouse. MCP-servern, webappen och
kontrollpanelen *läser* (kontrollpanelen kör dessutom ETL-scripten som
subprocesser). Webappen är den driftsatta produkten i Azure; `gui.py` är
lokal operatörstooling och körs inte i containern.

---

## 2. Lagerkarta — de facto-paketen i den platta layouten

Rotmappen ser platt ut men koden är i praktiken lagerindelad. Importgrafen
respekterar redan de gränser som mappar skulle rita:

```
Lager 0 — Grund
  shared.py   config, loggning (stdout + JSONL), Dotterbolagslista-läsare,
              filflytt-helpers, INL.xlsx-skrivare, Azure Blob-fallback.
              Ingen DB, ingen domänlogik. Importeras av nästan allt.
  db.py       Postgres-anslutning (Conn-wrapper), hela schema-DDL:n,
              dim-synk. Importerar shared.

Lager 1 — Extraktion            extract.py, dry_run.py
  .msg-mail → extracted/{period}/{Country}/      (importerar shared)

Lager 2 — Landsbearbetning      process_{sweden,norway,finland,denmark,germany}.py
  extracted/ → output/ (INL.xlsx / omdöpt SIE/SAF-T)
  Importerar ENDAST shared. Rör aldrig databasen.

Lager 3 — Warehouse-laddare     load_{sie,saft,inl,ib,personnel,suppliers,
  output/ → Postgres             exchange_rates,account_map}.py
  Importerar db + shared.        load_history_excel.py, load_history_sie_saft.py
                                 delete_db.py

Lager 4 — Konsumenter av warehouse
  mcp_server.py        egen connection pool
  webapp/backend/      egen connection pool, återanvänder db.Conn
  gui.py + gui_status/gui_runner/gui_overrides

Tvärgående — analys/verifiering
  verify_facit.py, check_counterparties.py, suggest_categories.py
  scripts/  (bootstrap, migration, smoke-test, ad-hoc-diagnostik)

Orkestrering
  run_all.py  kör alla process-script i följd (subprocess)
  reset.py    återställer filer för omkörning
```

---

## 3. Beroendegraf

```
                          shared.py
                         /    |     \
                        /     |      \
              process_*.py   db.py   extract.py / dry_run.py
                               |
                  ┌────────────┼─────────────┐
                  |            |             |
              load_*.py   mcp_server.py   webapp/backend/
                  |        (egen pool)    (egen pool, db.Conn)
        load_history_sie_saft.py
          → importerar load_sie + load_saft

  gui.py → gui_status, gui_runner, gui_overrides, shared
  verify_facit.py → load_inl (återanvänder read_inl_rows)
  webapp/backend/counterparty_data.py → check_counterparties (rotfil)
  scripts/* → db, load_sie m.fl. (kräver repo-roten på sys.path)
```

**Inga importcykler.** `shared` och `db` är medvetna nav, inte
oavsiktliga god-moduler. Lagerföljden (processorer→shared; laddare→db+shared;
db→shared) hålls konsekvent.

---

## 4. Datalager

Postgres-stjärnschema, definierat i `db.py` (`SCHEMA_SQL`):

- **Faktatabeller:** `fact_balances` (kärnan — saldon per bolag/period/konto),
  `fact_journal_sie`, `fact_journal_saft` (verifikatrader), `fact_personnel`,
  `fact_supplier_spend`.
- **Dimensioner:** `dim_company`, `dim_period`, `dim_account_map`,
  `dim_exchange_rate`, `dim_supplier_register`.
- **Stöd:** `backup_from_mercur` (facit-jämförelse), `load_history` (laddlogg).

Schemat har en inbyggd migrationsväg: `db._migrate()` lägger till kolumner
som tillkommit efter initialschemat och döper om `source_kind 'INL' → 'IMP'`.
Idempotent, körs vid varje `init_schema()`.

`SCHEMA.md` i roten dokumenterar schemat för MCP-konsumenter.

---

## 5. Flaggade observationer

Faktiska iakttagelser, inte åtgärdsförslag. Grovt sorterade efter hur mycket
de påverkar.

| # | Observation | Konsekvens |
|---|-------------|-----------|
| 1 | **Tre oberoende DB-anslutningsimplementationer.** `db.connect()` (laddare, enkel anslutning), `mcp_server._get_pool()` (egen pool + Key Vault-fallback) och `webapp...lifespan` (egen pool). `DATABASE_URL` resolvas på tre ställen; två av dem lägger på en KV-fallback var för sig. `mcp_server.py` använder inte `db.py` alls. | Anslutnings-/pool-/URL-logik finns i triplikat. Ändring av t.ex. timeout-policy måste göras på tre ställen. |
| 2 | **Webappen är inte fristående.** `webapp/backend/*` kör `sys.path.insert(0, REPO)` och importerar sedan `db`, `from shared import …` och `from check_counterparties import …`. `# noqa: E402` på varje korsimport. | Webapp-paketet når *uppåt* i rotmodulerna. En rotfil (`check_counterparties.py`) används som bibliotek av webappen. Detta är den rörigaste sömmen i kodbasen. |
| 3 | **`Conn`-wrappern efterliknar det gamla DuckDB-API:t.** Dess docstring säger att den finns för att "minimera diff i kallande kod" mot DuckDB-API:t. | DuckDB→Postgres-migrationen är klar; wrappern ligger kvar som kompatibilitetslager mellan kallande kod och psycopg:s cursor-modell. |
| 4 | **DuckDB-rester.** `duckdb` ligger kvar i `requirements.txt` (motiverat: `scripts/migrate_duckdb_to_postgres.py` läser den gamla filen). `data/finance.duckdb` är stale (känt). | Beroende + script kan tas bort först när DuckDB är slutgiltigt avvecklat. |
| 5 | **Stora filer.** `process_finland.py` 1224 rader, `load_sie.py` 827, `load_personnel.py` 694, `process_germany.py` 659, `gui.py` 655, `load_saft.py` 644. `process_finland.py` innehåller 12 läsarformat (A–L) och en `run_NNN()` per finskt bolag. `load_sie.py` rymmer SIE-parsning, #PSALDO/#VER-hantering, NAV-quirk och YTD-kumulering i en fil. | Filstorlekarna följer av mängden distinkt funktionalitet per fil — Finland speglar antalet stödda exportformat. |
| 6 | **Konfiguration är spridd.** `config.json` (base_path), `_params/overrides.json` + `facit_overrides.json`, `webapp/config/*.yaml` (pnl_kpis, pnl_layout). JSON och YAML på olika ställen. | Ingen enskild plats att se "all konfiguration". |
| 7 | **Två komplementära frontends.** PySide6 `gui.py` = lokal operatörspanel (kör/övervakar ETL). React-webappen = driftsatt rapportvisare. CLAUDE.md noterar att `gui.py` inte körs i App Service-containern. | De tjänar olika syften — operatörspanel vs. rapportvisare — och är inte duplicering. |

---

## 6. `scripts/` — färskhetsbedömning

`scripts/` (20 filer) har två tydligt skilda populationer. Bedömningen
nedan bygger på git-historik (antal commits, senaste ändring) plus
filnamnsmönster — den slutliga behåll/radera-frågan är din.

**Återkommande verktyg — i bruk:**

| Fil | Senast | Roll |
|-----|--------|------|
| `smoke_test_sql.py` | 2026-05-21 (3c) | SQL-koll före push, etablerat flöde |
| `push_master.py` | 2026-05-07 | Pushar Dotterbolagslistan till Azure Blob |
| `migrate_duckdb_to_postgres.py` | 2026-05-07 | Migrationskälla (behåll tills DuckDB avvecklas) |
| `load_fi_jan_from_mercur.py` | 2026-05-19 | Dokumenterad Fennoa-januari-workaround |
| `compare_all_file_vs_db.py` / `compare_se_file_vs_db.py` | 2026-05-21 | Fil-vs-DB-avstämning, färska |
| `check_saft_journal_dups.py` | 2026-05-21 | Kopplad till öppen SAF-T-dedup-städning |
| `bootstrap.ps1` / `bootstrap_mcp.ps1` | 2026-05-11/13 | Miljöuppsättning |

**Engångsdiagnostik — kandidater för borttag.** Skapade 2026-05-15,
1 commit var, aldrig rörda sedan. Filnamnen avslöjar dem — enskilda
bolagsnummer (`195`, `153`, `216`) eller period (`202604`) inbakade =
byggda för att svara på en fråga en gång:

- `check_195.py`, `check_22xx.py`, `check_inl_sums.py`, `check_202604_sums.py`
- `compare_153.py`, `diff_fi.py`, `inspect_216.py`, `inspect_fi.py`
- `run_2026_phase3.ps1` (2026-05-12, 1c) — sannolikt engångs-fas-körare, verifiera

`check_orgnr_parse.py` och `check_sie_ver.py` ser också ad-hoc ut men är
nyare (05-20/05-21) och kan fortfarande vara relevanta.

`webapp/scripts/` har ytterligare 4 dev-script (`diag.py`, `read_order.py`,
`verify_inl.py`, `verify_mercur.py`) med samma "används detta än?"-fråga.

---

## 7. Test- och CI-läge

- **Noll pytest-tester.** Ingen `tests/`-mapp, inga `test_*.py`.
- **De facto-skyddsnät:** `verify_facit.py` (jämför INL-utfall mot känt facit),
  `scripts/smoke_test_sql.py`, `scripts/compare_*_file_vs_db.py` och
  `backup_from_mercur`-tabellen för facit-jämförelse. Adekvat *strategi* för
  en datapipeline (golden-output-jämförelse är legitimt) — men det är manuellt
  och ad-hoc, ingen grind. Inget körs vid commit.
- **CI/CD:** `.github/workflows/deploy.yml` + `deploy-mcp.yml` deployar
  App Service-containrar. Ingen test- eller lint-workflow — CI deployar men
  verifierar inte.

---

## 8. Bilaga — filinventering (rot)

| Grupp | Filer |
|-------|-------|
| Grund | `shared.py`, `db.py` |
| Extraktion | `extract.py`, `dry_run.py` |
| Landsbearbetning | `process_sweden/norway/finland/denmark/germany.py` |
| Laddare | `load_sie/saft/inl/ib/personnel/suppliers/exchange_rates/account_map.py`, `load_history_excel.py`, `load_history_sie_saft.py`, `delete_db.py` |
| Konsumenter | `mcp_server.py`, `gui.py`, `gui_status.py`, `gui_runner.py`, `gui_overrides.py` |
| Analys/verifiering | `verify_facit.py`, `check_counterparties.py`, `suggest_categories.py` |
| Orkestrering | `run_all.py`, `reset.py` |
| Webapp | `webapp/backend/` (7 moduler + 7 SQL-filer), `webapp/frontend/` (React/Vite/TS, 9 komponenter), `webapp/config/` (2 YAML) |
| Övrigt i roten | `start-mcp.ps1`, `compose.yml`, `requirements.txt`, `Run extract files.txt` (löst textfragment — mellanslag i filnamnet) |

---

*Genererad av en läsande arkitekturgenomgång — ingen kod ändrades.*
