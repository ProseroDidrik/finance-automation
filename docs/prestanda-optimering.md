# Prestanda-optimering — finance-warehouse (MCP + GUI)

**Status:** Specifikation, genomförandeklar
**Skapad:** 2026-05-21
**Författare:** Didrik Wachtmeister + Claude
**Omfattning:** Latens för (a) Claude-frågor mot warehouse via MCP-servern och
(b) Finance Reporting GUI:t. Postgres-, Azure- och kodnivå.

---

## 1. Sammanfattning

Både Claude-frågorna och GUI:t upplevs som långsamma — *både trög start och
segt sen*. Orsakerna är strukturella och kända, inte mystiska:

1. **Cold start** — `alwaysOn=false` på båda App Service-apparna. Containern
   avlastas efter 20 min idle; varje ny Claude-konversation och varje GUI-besök
   efter en paus betalar en full container-uppstart.
2. **Delad undermålig plan** — MCP-servern och GUI-backenden kör på *samma*
   B1-plan (1 kärna, 1,75 GB RAM) och konkurrerar om CPU.
3. **Postgres på minsta tier** — `Standard_B1ms` Burstable, 32 GB, **120 IOPS**.
   IOPS-taket gör tunga frågor (täckningsmatris, journal-scan) 1–14 s.
4. **Kodnivå-spill** — ny DB-anslutning öppnas per anrop (ingen pool),
   `describe_schema` kör exakt `COUNT(*)` på varje tabell, SQL-filer läses från
   disk per request.

Åtgärderna är indelade i tre faser. **Fas 0 är gratis** och har störst
hävstång. **Fas 1 kostar ~+39 USD/mån** (inom "måttlig" budget). **Fas 2**
tas bara om mätning efter Fas 0–1 visar att det behövs.

> **Vad detta inte löser:** se [avsnitt 4](#4-vad-detta-inte-löser). Själva
> Claude.ai-modellens tänk-tid och MCP-connectorns round-trip ligger utanför
> det denna spec kan påverka.

---

## 2. Bakgrund & mål

`finance-warehouse` är en Postgres-databas (Azure Database for PostgreSQL
Flexible Server) som nås på två sätt:

- **MCP-servern** (`mcp_server.py`, App Service `app-finauto-mcp-6427`) —
  exponerar `describe_schema` + `query_sql` som Custom Connector i Claude.ai /
  Claude Desktop.
- **GUI:t** (`webapp/`, FastAPI-backend + React-frontend, App Service
  `app-finauto-6427`) — P&L-rapporter, täckningsmatris, personal, leverantörer.

**Mål med optimeringen:**

| Mätpunkt | Idag (uppskattat) | Mål efter Fas 0–1 |
|---|---|---|
| Cold start (första anropet efter idle) | 30–60 s | ~0 (containern hålls varm) |
| `describe_schema` (varje ny konversation) | 10–30 s | < 1 s |
| Median DB-query, steady state | 100–400 ms | < 150 ms |
| Tung query (täckningsmatris, journal-scan) | 1–14 s | < 4 s |
| Median GUI-sidladdning | flera sekunder | < 1,5 s |

---

## 3. Diagnos

### 3.1 Live Azure-tillstånd (mätt 2026-05-21)

```
Postgres  psql-finauto-6427 : Standard_B1ms, Burstable, 32 GB, 120 IOPS,
                              v16, ingen HA, autoGrow av
App-plan  asp-finauto-6427  : B1 (Basic) — delas av webapp OCH MCP
Webapp    app-finauto-6427  : alwaysOn = false
MCP       app-finauto-mcp-6427 : alwaysOn = false
```

- **120 IOPS** är ett hårt tak. På Flexible Server (Premium SSD) ges
  *provisionerad* IOPS av lagringsstorleken: 32 GiB → 120, 64 GiB → 240,
  128 GiB → 500, 256 GiB → 1 100. 32 GiB ger alltså golvet. Effektiv IOPS =
  min(lagringens provisionerade, compute-tierns tak — B1ms = 640).
- **B1 = 1 kärna, 1,75 GB RAM.** `bootstrap_mcp.ps1` återanvänder webappens
  plan (`$Plan`), så två Python-webbservrar delar en kärna.
- **`alwaysOn=false`** → container-avlastning efter 20 min idle.
- **`healthCheckPath`** är inte satt på någon av apparna.

### 3.2 Bevis: MCP-query-loggen

`mcp_server.py` loggar varje `query_sql` till `_logs/mcp_queries.jsonl` med
`{sql, rows, ms, ok}`. Skärmning av loggen (270 rader):

- **Median ~100–400 ms** — steady state är acceptabelt för enkla frågor.
- **Tung svans:** enskilda frågor på 1 084 / 2 190 / 2 795 / 3 211 / 13 821 ms.
- Den värsta (13,8 s) var en `COUNT(*)` på `fact_journal_saft` utan
  periodfilter → full tabell-scan, IOPS-bunden.

`ms`-värdet mäts serverside och *inkluderar* anslutningsetableringen. Loggen
fångar **inte** cold start (sker före Python-koden) och inte `describe_schema`
(loggas inte). De två osynliga posterna är troligen den största delen av den
upplevda slöheten.

### 3.3 Kodnivå-fynd

| Fil:rad | Fynd | Konsekvens |
|---|---|---|
| `mcp_server.py:117` `_connect()` | Ny `psycopg.connect()` per tool-anrop | TCP+TLS+auth-handshake (~50–150 ms) varje gång |
| `mcp_server.py:152-154` | `describe_schema` kör `SELECT COUNT(*)` på *varje* tabell i loop | Exakt count = seq-scan; ~15 tabeller på burstable disk = 10–30 s, varje ny konversation |
| `db.py:157` `connect()` / `main.py:69` `open_db()` | Ny anslutning per HTTP-request; vissa endpoints öppnar 2× | Handshake-overhead × antal anrop per sidladdning |
| `main.py:221,328,374,471,654,936,962` | SQL-filer `.read_text()` läses från disk i varje endpoint-anrop | Onödig disk-I/O per request |
| `main.py:144,164` `/api/companies`, `/api/periods` | Långsamt föränderlig data hämtas färskt varje gång | Frontend kör dem vid varje sidladdning |
| `main.py:704-735` | Pivot-KPI:er beräknas i en O(bolag × buckets × konton) Python-loop | Kan bli sekunder ren Python vid stora pivots |
| `compare_coverage.sql` | Scannar `fact_journal_sie`/`saft` på `period`-intervall *utan* company-filter | Befintliga index är `(company_id, period)` — kan inte stödja predikat enbart på `period` |

### 3.4 Rotorsaker, rankade

1. **Cold start** (`alwaysOn=false`) — träffar *varje* ny session. Störst
   upplevd effekt, gratis att åtgärda.
2. **`describe_schema` exakt COUNT** — träffar *varje* ny Claude-konversation.
   Gratis att åtgärda.
3. **Anslutning per anrop** — träffar *varje* anrop. Mest kännbart i GUI:t som
   kedjar många små frågor. Gratis att åtgärda.
4. **Postgres IOPS/RAM-tak** — träffar de tunga frågorna. Kostar pengar.
5. **Delad CPU-plan** — träffar samtidig last. Kostar pengar.

---

## 4. Vad detta INTE löser

Var ärlig mot förväntningarna:

- **Claude.ai-modellens tänk-tid.** När du ställer en fråga i Claude går
  merparten av väggtiden åt till att modellen resonerar och gör
  tool-anrop-rundor — inte till SQL:en. Azure-fixarna gör varje query 2–10 s
  snabbare men gör inte Claude *självt* momentant.
- **MCP-connectorns round-trip.** Claude.ai → MCP-server → Postgres → tillbaka
  har nätverkshopp som inte kan optimeras bort härifrån.
- **Konsekvens:** efter Fas 0–1 ska cold start och query-svansen vara borta,
  men en *flerstegsanalys* i Claude tar fortfarande den tid Claude tar. Mät
  rätt sak — jämför enskild query-latens, inte "kändes hela konversationen
  snabb".

En icke-teknisk hävstång som hjälper: se [avsnitt 9](#9-användningsmönster-på-claude-sidan).

---

## 5. Åtgärdspaket

Varje åtgärd: **Vad / Varför / Var / Hur / Effort / Kostnad / Effekt.**
`az`-kommandon förutsätter `az login` och rätt subscription vald.

### Fas 0 — Gratis (kod + inställningar)

#### F0-1 · Slå på Always On

- **Vad:** Hindra App Service att avlasta containern vid idle.
- **Varför:** Eliminerar cold start — rotorsak #1.
- **Var:** `app-finauto-6427` och `app-finauto-mcp-6427`.
- **Hur:**
  ```bash
  az webapp config set -g rg-finauto-6427 -n app-finauto-6427     --always-on true
  az webapp config set -g rg-finauto-6427 -n app-finauto-mcp-6427 --always-on true
  ```
- **Effort:** 2 min. **Kostnad:** 0 (gratis från Basic-tier).
- **Effekt:** Cold start (~30–60 s) försvinner för stationär drift.
- **Not:** Två varma containrar på en B1 är OK minnesmässigt (~300–600 MB av
  1,75 GB). CPU-konkurrens vid *samtidig* last hanteras av F1-1.

#### F0-2 · Connection pooling

- **Vad:** Återanvänd DB-anslutningar i stället för att öppna en ny per anrop.
- **Varför:** Tar bort handshake-overhead (~50–150 ms) per anrop — rotorsak #3.
- **Var:** `mcp_server.py` (`_connect`, används av båda tools) och webappens
  läsväg `main.py:open_db()` → `db.connect(read_only=True)`.
- **Hur:**
  - Lägg till `psycopg_pool` i `requirements.txt`.
  - **MCP:** skapa en modulglobal `ConnectionPool` (öppnas vid start), byt
    `_connect()` mot `pool.connection()`-context i `describe_schema`/`query_sql`.
  - **Webapp:** skapa poolen i `lifespan` (`main.py:58`), byt `open_db()` att
    hämta ur poolen.
  - Liten pool: `min_size=1, max_size=4–6` per app. B1ms har lågt
    `max_connections` (~50 totalt, delat med loaders).
  - Sätt `statement_timeout` via pool-`configure`-callback så varje uthyrd
    anslutning är korrekt konfigurerad.
  - **Rör inte loader-vägen** (`db.connect(read_only=False)`) — loaders kör
    bulk-skrivningar med explicита transaktioner och ska inte poolas.
- **Effort:** ~halvdag. **Kostnad:** 0.
- **Effekt:** Störst enskild "segt sen"-fix för GUI:t (många små anrop/sida).
- **Risk:** I `query_sql` kan en timeout/`conn.cancel()` lämna anslutningen i
  oklart skick — kassera den anslutningen ur poolen i stället för att återlämna
  den (psycopg_pool `check`-callback eller explicit `pool.putconn(..., close)`).
- **Risk:** Med Always On håller varje app sin pool öppen permanent (~4–6
  anslutningar/app). Plus loaders och lokala dev-sessioner närmar sig B1ms
  anslutningstak. Verifiera `SHOW max_connections;` efter att pooling införts —
  höj via Azure server-parameter om den ligger lågt (< ~50).

#### F0-3 · `describe_schema` — approx-count + cache

- **Vad:** Sluta räkna exakta radantal; cacha hela svaret.
- **Varför:** `describe_schema` anropas i början av varje konversation;
  exakt `COUNT(*)` × ~15 tabeller på burstable disk = 10–30 s — rotorsak #2.
- **Var:** `mcp_server.py:128-170`, loopen `mcp_server.py:152-154`.
- **Hur:**
  - Byt per-tabell-loopen mot en enda query:
    ```sql
    SELECT relname, reltuples::bigint AS approx_rows
    FROM pg_class
    WHERE relkind = 'r' AND relnamespace = 'public'::regnamespace
    ORDER BY relname;
    ```
  - Cacha hela retursträngen i en modulvariabel med tidsstämpel; returnera
    cachad kopia om < 5–10 min gammal.
  - `reltuples` är ungefärligt (uppdateras av `ANALYZE`/autovacuum). Skriv
    "≈ rader (uppskattning)" i tabellrubriken så ingen tror det är exakt.
- **Effort:** 1–2 h. **Kostnad:** 0.
- **Effekt:** `describe_schema` < 1 s; 0 DB-arbete vid cache-träff.

#### F0-4 · Ladda SQL-filer + cacha långsam data

- **Vad:** Läs SQL-filer en gång; cacha `companies`/`periods` kort.
- **Varför:** Onödig disk-I/O per request; frontend frågar
  companies/periods vid varje sidladdning.
- **Var:** `main.py` — SQL-`.read_text()` på rad 221/328/374/471/654/936/962;
  `/api/companies` (144), `/api/periods` (164).
- **Hur:**
  - Läs alla `webapp/backend/sql/*.sql` en gång vid modulimport till
    konstanter/dict.
  - In-process cache (TTL ~5 min) för `/api/companies` och `/api/periods` —
    de ändras bara när ny data laddas (månadsvis).
- **Effort:** 2–3 h. **Kostnad:** 0.
- **Effekt:** Disk-I/O bort per request; companies/periods momentana vid träff.

#### F0-5 · Saknade Postgres-index på `period`

- **Vad:** Index på `fact_journal_sie(period)` och `fact_journal_saft(period)`.
- **Varför:** `compare_coverage.sql` filtrerar journal-tabellerna på
  `period`-intervall utan company-filter; `(company_id, period)`-indexen
  hjälper inte ett rent `period`-predikat.
- **Var:** `db.py` `SCHEMA_SQL` (rad ~257-259, ~282-284) + live-DB.
- **Hur:**
  ```sql
  CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_fjs_period
      ON fact_journal_sie(period);
  CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_fjsaft_period
      ON fact_journal_saft(period);
  ```
  Lägg även in (utan `CONCURRENTLY`) i `SCHEMA_SQL` så nya miljöer får dem.
  Kör `CONCURRENTLY` mot live-DB för att inte låsa tabellen.
- **Effort:** 15 min. **Kostnad:** 0 (marginell lagring).
- **Effekt:** Index-stöd för täckningssidans periodfilter.
- **Verifiera:** kör `EXPLAIN ANALYZE` på `compare_coverage.sql` före/efter.
  Om perioderna är lågselektiva (få distinkta perioder, många rader/period)
  ger indexet mindre — då är IOPS/RAM (Fas 1) den verkliga fixen.
- **Not:** Kör `ANALYZE` på journal-tabellerna efter stora laddningar (eller
  förlita dig på autovacuum) — `compare_coverage.sql` är planerar-känslig.

#### F0-6 · healthCheckPath + pg_stat_statements

- **Vad:** Health check-väg på apparna; query-statistik på Postgres.
- **Varför:** Auto-omstart av ohälsosamma instanser; underlag för att hitta de
  faktiskt långsammaste frågorna.
- **Hur:**
  ```bash
  az webapp config set -g rg-finauto-6427 -n app-finauto-mcp-6427 \
      --health-check-path /healthz
  az webapp config set -g rg-finauto-6427 -n app-finauto-6427 \
      --health-check-path /api/health
  az postgres flexible-server parameter set -g rg-finauto-6427 \
      -s psql-finauto-6427 --name shared_preload_libraries \
      --value pg_stat_statements        # kräver omstart av servern
  ```
- **Effort:** 30 min. **Kostnad:** 0.
- **Effekt:** Observability + self-healing.

#### F0-7 · Komprimera API-svar (GZip)

- **Vad:** Slå på gzip-komprimering för HTTP-svaren.
- **Varför:** Pivot- och täckningssvaren är stora JSON-objekt; FastAPI
  komprimerar inte by default.
- **Var:** `webapp/backend/main.py` — middleware-registreringen (rad ~81-91).
- **Hur:** Lägg till Starlettes `GZipMiddleware`
  (`app.add_middleware(GZipMiddleware, minimum_size=1000)`). Stdlib-/Starlette-
  nivå — ingen ny infra, inget nytt beroende.
- **Effort:** 10 min. **Kostnad:** 0.
- **Effekt:** Halverar typiskt överföringstiden för de tunga endpointsen.

### Fas 1 — Måttlig kostnad (~+39 USD/mån)

#### F1-1 · Egen App Service-plan till MCP-servern

- **Vad:** Flytta MCP-appen till en egen B1-plan.
- **Varför:** Idag delar MCP och GUI en kärna (rotorsak #5). En tung
  GUI-pivot fryser en samtidig Claude-fråga.
- **Var:** `app-finauto-mcp-6427`, idag på `asp-finauto-6427`.
- **Hur:**
  ```bash
  az appservice plan create -g rg-finauto-6427 -n asp-finauto-mcp-6427 \
      --is-linux --sku B1
  az webapp update -g rg-finauto-6427 -n app-finauto-mcp-6427 \
      --plan asp-finauto-mcp-6427
  ```
  Uppdatera `scripts/bootstrap_mcp.ps1` så `$Plan` för MCP pekar på den egna
  planen (annars återskapas delningen vid nästa körning).
- **Effort:** 30 min + verifiering. **Kostnad:** ~+13 USD/mån.
- **Effekt:** GUI och MCP slåss inte om CPU; båda kan hållas varma med
  egen kärna.

#### F1-2 · Postgres-lagring 32 → 128 GB

- **Vad:** Höj lagringen — IOPS skalar med storleken.
- **Varför:** 120 IOPS är det hårda taket på query-svansen (rotorsak #4).
- **Hur:**
  ```bash
  az postgres flexible-server update -g rg-finauto-6427 \
      -n psql-finauto-6427 --storage-size 128
  ```
- **Effort:** 15 min. **Kostnad:** ~+11 USD/mån.
- **Effekt:** Provisionerad IOPS 120 → **500** (~4,2×). Direkt på journal-scan
  / täckningsmatris. B1ms-tiern har ett IOPS-tak på 640, så 128 GiB:s 500 IOPS
  är fullt användbara redan utan tier-bytet (F1-3).
- **Varning:** Lagringsuppskalning är **enkelriktad** — kan inte krympas.
  Kort omstart kan krävas.

#### F1-3 · Postgres B1ms → B2s

- **Vad:** Ett tier-steg upp inom Burstable.
- **Varför:** B1ms har 2 GB RAM → liten cache → mycket går till disk. B2s ger
  2 vCore + 4 GB RAM.
- **Hur:**
  ```bash
  az postgres flexible-server update -g rg-finauto-6427 \
      -n psql-finauto-6427 --sku-name Standard_B2s --tier Burstable
  ```
- **Effort:** 15 min + nedtidsfönster (omstart, några min). **Kostnad:**
  ~+15 USD/mån.
- **Effekt:** Dubblad RAM (2 → 4 GiB) → större effektiv cache, färre
  diskläsningar; 2 vCore → parallellare planer, högre Burstable-baslinje,
  mer connection-headroom.
- **Obs:** B2s höjer compute-tierns IOPS-tak till 1 280, men *provisionerad*
  IOPS styrs av lagringen — med 128 GiB är den fortfarande 500. F1-3 är alltså
  en RAM/CPU-uppgradering, inte en IOPS-uppgradering. Vill man förbi 500 IOPS
  krävs 256 GiB lagring (1 100 IOPS, väl inom B2s 1 280-tak) — gör det bara om
  mätning visar att 500 IOPS är taket.
- **Efteråt:** verifiera `shared_buffers`, `work_mem`, `effective_cache_size`
  (Flexible Server sätter SKU-defaults — kontrollera att de följde med).
- **Ratt:** B2ms (8 GiB RAM, 1 920 IOPS-tak, dyrare) om RAM fortfarande är
  taket efter B2s.

### Fas 2 — Bara om mätning kräver det

#### F2-1 · Postgres → General Purpose

- Burstable-tiern har ett CPU-credit-tak — vid uthållig last stryps CPU:n.
  Mät **"CPU Credits Remaining"** i Azure Metrics. Om den bottnar regelbundet:
  byt till General Purpose (t.ex. `Standard_D2ds_v5`, 2 dedikerade vCore, 8 GB).
- **Kostnad:** ~+100–120 USD/mån netto över B2s — *över* "måttlig" budget,
  därför Fas 2.

#### F2-2 · Profilera pivot-KPI-loopen

- `main.py:704-735` beräknar KPI:er i en O(bolag × buckets × konton)
  Python-loop. Mät med timing-loggen (F0/avsnitt 7) först; optimera bara om
  det syns för stora pivots (helt land × månadsgranularitet).

---

## 6. Kostnadssammanställning

| Skede | Tillkommer | Total infra/mån |
|---|---|---|
| Idag | — | ~30–35 USD |
| Efter Fas 0 | +0 | ~30–35 USD |
| Efter Fas 1 | +13 (MCP-plan) +11 (lagring) +15 (B2s) ≈ **+39** | ~70–75 USD |
| Fas 2 (om) | +~100 (General Purpose) | ~170–175 USD |

Siffrorna är riktvärden för region Sweden Central — bekräfta i Azures
priskalkylator innan beslut.

---

## 7. Mätning & verifiering

Mät **före** Fas 0 och **efter** varje fas — annars vet man inte om en åtgärd
hjälpte.

- **MCP:** loggen finns redan (`_logs/mcp_queries.jsonl`, `ms`-fältet).
  Jämför fördelningen (median + p95) före/efter. *Obs:* loggen ligger i
  containerns filsystem och rensas vid omstart — exportera före deploy om en
  baslinje ska bevaras.
- **GUI:** lägg en enkel timing-middleware i `main.py` som loggar
  `{path, ms}` per request (samma JSONL-mönster som MCP). Idag finns **ingen**
  mätning för GUI:t — det är ett mäthål.
- **Azure Metrics att bevaka:**
  - App Service: *Response Time*, *CPU Percentage*, *Memory Percentage*.
  - Postgres: *CPU Credits Remaining* (avgör om Fas 2 behövs), *IOPS*,
    *Memory percent*, *Storage percent*.
- **pg_stat_statements** (F0-6): efter ett par dagars drift, kör
  `SELECT query, calls, mean_exec_time FROM pg_stat_statements ORDER BY
  mean_exec_time DESC LIMIT 20;` för att se de faktiskt dyraste frågorna.
- **Acceptanskriterier:** se måltabellen i [avsnitt 2](#2-bakgrund--mål).

---

## 8. Rekommenderad genomförandeordning

1. **Mät baslinjen** — spara en kopia av `mcp_queries.jsonl`, klocka några
   GUI-sidladdningar manuellt.
2. **Fas 0**, i denna ordning (effekt först, billigast risk först):
   F0-1 (Always On) → F0-5 (index) → F0-6 (health + pg_stat_statements) →
   F0-3 (describe_schema) → F0-4 (SQL-cache) → F0-7 (GZip) → F0-2 (pooling).
3. **Mät igen.** Om cold start och describe_schema är borta och steady state
   räcker — *stanna här*, Fas 1 kan vänta.
4. **Fas 1:** F1-1 (MCP-plan) är oberoende och kan göras när som helst.
   F1-2 + F1-3 görs ihop i ett nedtidsfönster (meddela testarna i förväg).
5. **Mät igen.** Fas 2 endast om *CPU Credits Remaining* visar att
   Burstable-taket är den kvarvarande flaskhalsen.

Kör `py scripts/smoke_test_sql.py` mot Azure-DB efter SQL-ändringar (F0-3,
F0-5) innan commit — fångar placeholder-buggar utan deploy-cykel.

---

## 9. Användningsmönster på Claude-sidan

Gratis hävstång som ligger utanför infran men påverkar upplevd hastighet:

- **Kör frågorna i en Claude Project** med projektinstruktionerna från
  `docs/mcp_connector.md`. En lös chatt utan kontext får ofta Claude att
  gissa schemat, få fel, och göra om — varje misslyckad runda är ett extra
  MCP-anrop. Project-instruktionerna gör att `describe_schema` körs en gång
  och `query_sql` blir rätt på första försöket.
- Efter F0-3 är `describe_schema` billig — men färre *onödiga* tool-rundor
  spar mer väggtid än något infra-steg, eftersom varje runda är en hel
  modell-inferens.

---

## 10. Risker & förbehåll

| Risk | Hantering |
|---|---|
| Felhanterad connection-pool → läckta anslutningar | Sätt timeouts; kassera anslutningar efter `cancel()`; testa lokalt mot Azure-DB |
| Lagringsuppskalning är irreversibel | Bekräfta 128 GB är rimligt långsiktigt innan F1-2 |
| Tier-/lagringsändring = kort nedtid | Kör utanför arbetstid; meddela testarna (Eva, Erik) |
| `reltuples` är ungefärligt | Märk kolumnen "≈" i `describe_schema` |
| `CREATE INDEX` på live-DB | Använd `CONCURRENTLY` — låser inte tabellen |
| Always On + två appar på en B1 fram till F1-1 | OK minnesmässigt; CPU-konkurrens vid samtidig last tills planen delas |
| `az`-ändringar i blandad privat tenant | Plan-flytt/resize kräver inga role assignments — undviker den kända `az role assignment`-buggen |

---

## Bilaga A — Snabbreferens, alla kommandon

```bash
# --- Fas 0 -----------------------------------------------------------------
az webapp config set -g rg-finauto-6427 -n app-finauto-6427     --always-on true
az webapp config set -g rg-finauto-6427 -n app-finauto-mcp-6427 --always-on true
az webapp config set -g rg-finauto-6427 -n app-finauto-mcp-6427 --health-check-path /healthz
az webapp config set -g rg-finauto-6427 -n app-finauto-6427     --health-check-path /api/health
az postgres flexible-server parameter set -g rg-finauto-6427 -s psql-finauto-6427 \
    --name shared_preload_libraries --value pg_stat_statements

# index (kör i psql mot live-DB)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_fjs_period   ON fact_journal_sie(period);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_fjsaft_period ON fact_journal_saft(period);

# --- Fas 1 -----------------------------------------------------------------
az appservice plan create -g rg-finauto-6427 -n asp-finauto-mcp-6427 --is-linux --sku B1
az webapp update -g rg-finauto-6427 -n app-finauto-mcp-6427 --plan asp-finauto-mcp-6427
az postgres flexible-server update -g rg-finauto-6427 -n psql-finauto-6427 --storage-size 128
az postgres flexible-server update -g rg-finauto-6427 -n psql-finauto-6427 \
    --sku-name Standard_B2s --tier Burstable
```

## Bilaga B — Berörda filer

| Fil | Åtgärd |
|---|---|
| `mcp_server.py` | F0-2 (pool), F0-3 (describe_schema) |
| `webapp/backend/main.py` | F0-2 (pool), F0-4 (SQL-cache + companies/periods-cache), F0-6 (timing-middleware), F0-7 (GZip) |
| `db.py` | F0-5 (index i `SCHEMA_SQL`) |
| `requirements.txt` | F0-2 (`psycopg_pool`) |
| `scripts/bootstrap_mcp.ps1` | F1-1 (egen `$Plan` för MCP) |
| Live Azure (App Service, Postgres) | F0-1, F0-6, F1-1, F1-2, F1-3 |

## Bilaga C — Källor

IOPS- och tier-siffrorna är verifierade 2026-05-21 mot Microsoft Learn:

- [Storage options — Azure Database for PostgreSQL Flexible Server](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-storage)
  (lagring → provisionerad IOPS: 32 GiB→120, 128 GiB→500, 256 GiB→1 100)
- [Compute Options — Azure Database for PostgreSQL Flexible Server](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-compute)
  (Burstable IOPS-tak: B1ms 640, B2s 1 280, B2ms 1 920)

Priserna i avsnitt 6 är riktvärden — bekräfta i
[Azure-priskalkylatorn](https://azure.microsoft.com/pricing/calculator/) för
region Sweden Central innan beslut.
