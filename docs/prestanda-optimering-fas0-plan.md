# Fas 0 Prestanda-optimering — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Genomför Fas 0 ur `docs/prestanda-optimering.md` — de gratis åtgärderna
som tar bort cold start och merparten av "segt sen" i MCP-servern och GUI:t.

**Architecture:** Sju oberoende åtgärder (F0-1…F0-7). Två är rena Azure-
inställningar (`az`), fem är kodändringar i tre filer (`mcp_server.py`,
`webapp/backend/main.py`, `db.py`) plus dependency-/Docker-justeringar. Varje
kodtask lämnar appen körbar och committas separat. Deploy sker via befintliga
GitHub Actions-workflows (`deploy.yml`, `deploy-mcp.yml`) när grenen mergas
till `main`.

**Tech Stack:** Python 3.12 (container) / FastAPI / psycopg 3 / `psycopg-pool` /
Azure App Service for Containers / Azure Database for PostgreSQL Flexible Server /
Azure CLI.

---

## Om verifiering — läs detta först

Repot har **inget pytest-/unittest-ramverk** (enda testverktyget är
`scripts/smoke_test_sql.py`, som kör SQL mot Azure-DB). Denna plan följer
repots faktiska mönster i stället för att tvinga in ett testramverk:

- **SQL-ändringar** verifieras med `py scripts/smoke_test_sql.py`.
- **Kodändringar** verifieras genom att köra apparna lokalt + `curl` / `py -c`.
- **Azure-ändringar** verifieras med `az ... show`.
- Varje kodtask avslutas med en **commit**.

Plats för planen: `docs/prestanda-optimering-fas0-plan.md` (denna fil).

## Förutsättningar

Innan Task 1 — verifiera att allt nedan stämmer:

- [ ] `az login` är gjort och rätt subscription är vald (`az account show`).
- [ ] DATABASE_URL finns i miljön (PowerShell):
  ```powershell
  $env:DATABASE_URL = (az keyvault secret show --vault-name kv-finauto-6427 --name database-url --query value -o tsv)
  ```
- [ ] Python-miljö med repots beroenden är aktiv (`.venv`).
- [ ] `gh` CLI är inloggad (`gh auth status`) — behövs i Task 10.

## Filöversikt

| Fil | Ansvar | Tasks |
|---|---|---|
| *(Azure-resurser)* | App Service + Postgres-konfiguration | 2, 3, 4, 10 |
| `db.py` | + `period`-index i `SCHEMA_SQL`, `close_cursor()`, `database_url()` | 4, 7 |
| `mcp_server.py` | `describe_schema` approx+cache; connection pool | 5, 6 |
| `webapp/Dockerfile.mcp` | + `psycopg-pool` i pip-listan (MCP-containern) | 6 |
| `requirements.txt` | + `psycopg-pool` (webapp-containern + lokal dev) | 7 |
| `webapp/backend/main.py` | pool, SQL-/svars-cache, GZip | 7, 8, 9 |

**OBS:** `webapp/Dockerfile.mcp` pip-installerar en **explicit lista**, inte
`requirements.txt`. Lägger man bara till `psycopg-pool` i `requirements.txt`
kraschar MCP-containern vid import. Båda ställena måste uppdateras (Task 6 + 7).

---

## Task 1: Förberedelse — gren + baslinjemätning

**Files:** inga kodändringar.

- [ ] **Step 1: Skapa arbetsgren från `main`**

```bash
git fetch origin
git checkout -b perf/fas-0-optimering origin/main
```

- [ ] **Step 2: Spara baslinjen för MCP-loggen**

```bash
cp _logs/mcp_queries.jsonl _logs/baseline_mcp_queries.jsonl
```

Om `_logs/mcp_queries.jsonl` saknas lokalt — hoppa över; baslinjen tas då
kvalitativt i nästa steg.

- [ ] **Step 3: Klocka tre representativa åtgärder (stoppur) och anteckna**

Mät och skriv ner i en scratch-anteckning (används i Task 10 för jämförelse):
1. Första frågan i en *ny* Claude-konversation mot MCP-connectorn.
2. GUI: första sidladdningen efter > 20 min inaktivitet (cold start).
3. GUI: en P&L-rapport + täckningssidan när appen är varm.

Ingen commit i denna task.

---

## Task 2: F0-1 — Slå på Always On

**Files:** inga kodändringar (Azure-inställning).

- [ ] **Step 1: Slå på Always On för båda apparna**

```bash
az webapp config set -g rg-finauto-6427 -n app-finauto-6427     --always-on true
az webapp config set -g rg-finauto-6427 -n app-finauto-mcp-6427 --always-on true
```

- [ ] **Step 2: Verifiera**

```bash
az webapp config show -g rg-finauto-6427 -n app-finauto-6427     --query alwaysOn
az webapp config show -g rg-finauto-6427 -n app-finauto-mcp-6427 --query alwaysOn
```

Förväntat: båda returnerar `true`.

- [ ] **Step 3: Verifiera att apparna fortfarande svarar**

```bash
curl -s https://app-finauto-mcp-6427.azurewebsites.net/healthz
curl -s https://app-finauto-6427.azurewebsites.net/api/health
```

Förväntat: `ok` respektive `{"status":"ok"}`.

Ingen commit (infrastrukturändring).

---

## Task 3: F0-6 — Health check-väg + pg_stat_statements

**Files:** inga kodändringar (Azure-inställningar). Postgres startas om — kör i
ett lågtrafik-fönster och meddela testarna (Eva, Erik).

- [ ] **Step 1: Sätt health check-väg på båda apparna**

```bash
az webapp config set -g rg-finauto-6427 -n app-finauto-mcp-6427 --health-check-path /healthz
az webapp config set -g rg-finauto-6427 -n app-finauto-6427     --health-check-path /api/health
```

- [ ] **Step 2: Aktivera pg_stat_statements**

```bash
az postgres flexible-server parameter set -g rg-finauto-6427 -s psql-finauto-6427 \
    --name shared_preload_libraries --value pg_stat_statements
az postgres flexible-server parameter set -g rg-finauto-6427 -s psql-finauto-6427 \
    --name pg_stat_statements.track --value top
```

- [ ] **Step 3: Starta om Postgres så `shared_preload_libraries` läses in**

```bash
az postgres flexible-server restart -g rg-finauto-6427 -n psql-finauto-6427
```

- [ ] **Step 4: Verifiera att extensionen är laddad**

```bash
py -c "import os, psycopg; c=psycopg.connect(os.environ['DATABASE_URL']); c.execute('CREATE EXTENSION IF NOT EXISTS pg_stat_statements'); print(c.execute('SELECT count(*) FROM pg_stat_statements').fetchone()); c.close()"
```

Förväntat: en rad med ett heltal (≥ 0) — ingen `relation does not exist`-fel.

Ingen commit (infrastrukturändring).

---

## Task 4: F0-5 — Index på `fact_journal_*.period`

**Files:**
- Live Postgres: två `CREATE INDEX CONCURRENTLY`
- Modify: `db.py` — `SCHEMA_SQL`, nära de befintliga `idx_fjs_*` / `idx_fjsaft_*`

- [ ] **Step 1: Skapa indexen mot live-DB (utanför transaktion — `CONCURRENTLY`)**

```bash
py -c "import os, psycopg; c=psycopg.connect(os.environ['DATABASE_URL'], autocommit=True); c.execute('CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_fjs_period ON fact_journal_sie(period)'); c.execute('CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_fjsaft_period ON fact_journal_saft(period)'); print('index ok'); c.close()"
```

Förväntat: `index ok`.

- [ ] **Step 2: Lägg in indexen i `SCHEMA_SQL` (för nya miljöer)**

I `db.py`, i `SCHEMA_SQL`-strängen, lägg till en rad efter
`CREATE INDEX IF NOT EXISTS idx_fjs_account ON fact_journal_sie(account_code);`:

```sql
CREATE INDEX IF NOT EXISTS idx_fjs_period         ON fact_journal_sie(period);
```

Och en rad efter
`CREATE INDEX IF NOT EXISTS idx_fjsaft_account    ON fact_journal_saft(account_code);`:

```sql
CREATE INDEX IF NOT EXISTS idx_fjsaft_period         ON fact_journal_saft(period);
```

(`SCHEMA_SQL` körs som batch — `CONCURRENTLY` används *inte* här, bara mot
live-DB i Step 1.)

- [ ] **Step 3: Verifiera att täcknings-SQL fortfarande kör och mät**

```bash
py scripts/smoke_test_sql.py
```

Förväntat: `All 2 tests OK.` Notera ms-tiden för `compare_coverage.sql` —
jämför mot Task 10.

- [ ] **Step 4: Bekräfta att planeraren använder det nya indexet**

```bash
py -c "import os, psycopg; c=psycopg.connect(os.environ['DATABASE_URL']); body=open('webapp/backend/sql/compare_coverage.sql',encoding='utf-8').read().replace('@period_lo@','202601').replace('@period_hi@','202604'); cur=c.cursor(); cur.execute('EXPLAIN '+body); [print(r[0]) for r in cur.fetchall()]; c.close()" | findstr /i "idx_fjs"
```

Förväntat: minst en rad nämner `idx_fjs_period` eller `idx_fjsaft_period`.
Om inte — perioderna är lågselektiva; notera det, indexet skadar inte men
IOPS-bumpen (Fas 1) blir då den verkliga fixen.

- [ ] **Step 5: Commit**

```bash
git add db.py
git commit -m "perf(db): index på fact_journal_*.period för täckningssidan"
```

---

## Task 5: F0-3 — `describe_schema` approx-count + cache

**Files:**
- Modify: `mcp_server.py` — `describe_schema` (rad ~128-170) + nya modul-globaler.

- [ ] **Step 1: Ersätt `describe_schema` med helper + cache**

I `mcp_server.py`, ersätt hela `describe_schema`-funktionen (från `@mcp.tool()`
t.o.m. `return "\n".join(parts)`) med följande. Lägg cache-konstanterna direkt
ovanför:

```python
# describe_schema cachas — anropas i början av varje konversation och behöver
# inte vara färskare än så. reltuples är ungefärligt (uppdateras av ANALYZE).
SCHEMA_CACHE_TTL = 300.0  # sekunder
_schema_cache: tuple[float, str] | None = None
_schema_cache_lock = threading.Lock()


def _build_schema_snapshot() -> str:
    """Bygg describe_schema-svaret: SCHEMA.md + approx tabellöversikt + semantik.

    Tabellöversikten använder pg_class.reltuples (ungefärligt, ~momentant) i
    stället för exakt COUNT(*) per tabell, som annars seq-scannar varje tabell.
    """
    parts: list[str] = []
    if SCHEMA_MD.exists():
        parts.append(SCHEMA_MD.read_text(encoding="utf-8"))
    else:
        parts.append("(SCHEMA.md saknas)")

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname, c.reltuples::bigint
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'r' AND n.nspname = 'public'
            ORDER BY c.relname
            """
        )
        rows = cur.fetchall()

    parts.append("\n## Live snapshot (≈ rader, uppskattning)\n")
    parts.append("| Tabell | ≈ Rader |")
    parts.append("|---|---:|")
    for name, count in rows:
        parts.append(f"| `{name}` | {max(count, 0):,} |")

    if SEMANTICS_MD.exists():
        parts.append("\n\n---\n\n# Query-semantik (läs detta innan du skriver SQL)\n")
        parts.append(SEMANTICS_MD.read_text(encoding="utf-8"))

    return "\n".join(parts)


@mcp.tool()
def describe_schema() -> str:
    """Returnera warehouse-schema (SCHEMA.md), live tabellöversikt med approx
    radantal, plus query-semantik (warehouse_semantics.md) som täcker
    period_type, best_source-prioritet per land, scenario-filter,
    teckenkonvention och facit-jämförelse. Anropa detta FÖRST när du ska skriva
    en query — utan semantik-reglerna räknar du fel.

    Svaret cachas i 5 min — radantalen är ungefärliga (pg_class.reltuples).
    """
    global _schema_cache
    now = time.time()
    cached = _schema_cache
    if cached is not None and now - cached[0] < SCHEMA_CACHE_TTL:
        return cached[1]
    snapshot = _build_schema_snapshot()
    with _schema_cache_lock:
        _schema_cache = (time.time(), snapshot)
    return snapshot
```

`_build_schema_snapshot` använder den befintliga `_connect()` — Task 6 byter
den mot connection-poolen och tar bort `_connect()`.

- [ ] **Step 2: Verifiera lokalt att svaret byggs och cachas**

Med `DATABASE_URL` satt i miljön, kör:

```bash
py -c "import time, mcp_server; t=time.time(); s=mcp_server.describe_schema(); print('1:a anrop', round(time.time()-t,3),'s, len', len(s)); t=time.time(); mcp_server.describe_schema(); print('2:a anrop (cache)', round(time.time()-t,4),'s')"
```

Förväntat: första anropet < 2 s, andra anropet < 0,01 s (cache-träff). Utskriften
ska innehålla "Live snapshot".

- [ ] **Step 3: Commit**

```bash
git add mcp_server.py
git commit -m "perf(mcp): describe_schema approx-count + 5 min-cache"
```

---

## Task 6: F0-2a — Connection pooling i MCP-servern

**Files:**
- Modify: `mcp_server.py` — inför pool, ersätt `_connect()`-användning.
- Modify: `webapp/Dockerfile.mcp` — lägg `psycopg-pool` i pip-listan.

- [ ] **Step 1: Lägg till `psycopg-pool` i MCP-containerns pip-lista**

I `webapp/Dockerfile.mcp`, i `RUN pip install --no-cache-dir`-blocket, lägg
till en rad efter `"psycopg[binary]>=3.2" \`:

```dockerfile
        "psycopg-pool>=3.2" \
```

- [ ] **Step 2: Inför connection pool i `mcp_server.py`**

Lägg till importen vid de andra importerna (efter `import psycopg`):

```python
from psycopg_pool import ConnectionPool
```

Lägg till poolen och dess accessor direkt efter `_resolve_database_url()`-
funktionen. Behåll `_resolve_database_url` (poolen behöver conninfo):

```python
# Connection pool — återanvänder anslutningar i stället för att göra en ny
# TCP+TLS+auth-handshake per tool-anrop. configure() ger varje anslutning
# 30 s statement_timeout.
_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def _configure_conn(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(f"SET statement_timeout = {int(QUERY_TIMEOUT_SEC * 1000)}")


def _get_pool() -> ConnectionPool:
    """Lazy singleton-pool. Skapas vid första anropet (efter att DATABASE_URL
    resolvats, ev. via Key Vault). Fungerar i både stdio- och http-läget."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ConnectionPool(
                    conninfo=_resolve_database_url(),
                    min_size=1,
                    max_size=4,
                    kwargs={"autocommit": True},
                    configure=_configure_conn,
                    open=True,
                )
    return _pool
```

Behåll `_connect()` tills vidare — `_build_schema_snapshot` (Task 5) använder
den fortfarande; den tas bort i Step 4.

- [ ] **Step 3: Använd poolen i `query_sql`**

Ersätt `query_sql`-funktionens kropp från `t0 = time.time()` t.o.m. `finally`-
blocket med följande. `SET statement_timeout` görs nu av `_configure_conn`, så
det egna SET-blocket tas bort:

```python
    t0 = time.time()
    try:
        with _get_pool().connection() as conn:
            def _cancel() -> None:
                try:
                    conn.cancel()
                except Exception:
                    pass

            timer = threading.Timer(QUERY_TIMEOUT_SEC + 2, _cancel)
            timer.start()
            try:
                with conn.cursor() as cur:
                    cur.execute(sql_to_run)
                    columns = [d[0] for d in cur.description] if cur.description else []
                    rows = cur.fetchall()
                df = pd.DataFrame(rows, columns=columns)
            finally:
                timer.cancel()
    except Exception as exc:
        _log({"sql": sql, "ok": False, "error": str(exc)})
        return f"ERROR: {exc}"
```

(Resten av `query_sql` — `elapsed_ms`, `_log`, formatteringen — är oförändrad.)

- [ ] **Step 4: Byt `_build_schema_snapshot` till poolen och ta bort `_connect()`**

I `_build_schema_snapshot` (infördes i Task 5), ändra anslutningsraden:

```python
    # före:  with _connect() as conn, conn.cursor() as cur:
    with _get_pool().connection() as conn, conn.cursor() as cur:
```

Ta sedan bort funktionen `_connect()` — inga anropare kvarstår (`query_sql` och
`_build_schema_snapshot` använder nu poolen).

- [ ] **Step 5: Verifiera lokalt**

```bash
py -c "import mcp_server; print(mcp_server.query_sql('SELECT 1 AS x'))"
py -c "import time, mcp_server; t=time.time(); s=mcp_server.describe_schema(); print(round(time.time()-t,3),'s, len',len(s))"
```

Förväntat: en markdown-tabell utan traceback; `describe_schema` < 2 s och
utskriften innehåller "Live snapshot". (Om `@mcp.tool()` gör funktionen icke
direkt anropbar — anropa via `mcp_server.query_sql.fn(...)`; FastMCP returnerar
normalt originalfunktionen.)

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py webapp/Dockerfile.mcp
git commit -m "perf(mcp): connection pooling (psycopg-pool)"
```

---

## Task 7: F0-2b — Connection pooling i webappen

**Files:**
- Modify: `requirements.txt` — lägg till `psycopg-pool`.
- Modify: `db.py` — `database_url()` + `Conn.close_cursor()`.
- Modify: `webapp/backend/main.py` — pool i `lifespan`, ny `open_db()`.

- [ ] **Step 1: Lägg `psycopg-pool` + `tabulate` i `requirements.txt`**

I `requirements.txt`, efter raden `psycopg[binary]>=3.2`, lägg till:

```
psycopg-pool>=3.2
```

Lägg också till `tabulate` — pandas `df.to_markdown()` i `mcp_server.py` kräver
det. Det finns i `Dockerfile.mcp`-listan men saknas i `requirements.txt`; utan
det failar de lokala verify-stegen i Task 5–6 med ett missvisande
`ModuleNotFoundError`:

```
tabulate>=0.9
```

- [ ] **Step 2: Exponera `database_url()` och lägg till `close_cursor()` i `db.py`**

I `db.py`, lägg till en publik accessor direkt efter `_database_url()`:

```python
def database_url() -> str:
    """Publik accessor för DATABASE_URL — t.ex. för webappens connection pool."""
    return _database_url()
```

Lägg till en metod i klassen `Conn`, direkt efter `close()`:

```python
    def close_cursor(self) -> None:
        """Stäng den interna cursorn utan att stänga anslutningen.
        Används av poolade läsvägar där anslutningen återlämnas, inte stängs."""
        if self._cur is not None and not self._cur.closed:
            self._cur.close()
        self._cur = None
```

- [ ] **Step 3: Inför pool + ny `open_db()` i `main.py`**

I `webapp/backend/main.py`, lägg till importerna (vid de andra importerna):

```python
from contextlib import asynccontextmanager, contextmanager
from psycopg_pool import ConnectionPool
```

(`asynccontextmanager` importeras redan — slå ihop raden, lägg bara till
`contextmanager`.)

Lägg till en modul-global under `FRONTEND_DIST`-konstanten:

```python
_pool: ConnectionPool | None = None
```

Ersätt `lifespan`-funktionen med:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Öppna en connection pool vid uppstart och verifiera att Postgres svarar.
    Pool:en återanvänder anslutningar — ingen TCP+TLS-handshake per request."""
    global _pool
    _pool = ConnectionPool(
        conninfo=db.database_url(),
        min_size=1,
        max_size=6,
        kwargs={"autocommit": True},
        open=True,
    )
    with _pool.connection() as raw:
        raw.execute("SELECT 1")
    yield
    _pool.close()
```

Ersätt `open_db()`-funktionen med:

```python
@contextmanager
def open_db():
    """Låna en poolad read-only-anslutning, inkapslad som db.Conn.

    Anslutningen återlämnas till poolen (stängs inte). Endpoints använder den
    oförändrat: `with open_db() as con: con.fetch_dicts(...)`.
    """
    if _pool is None:
        raise RuntimeError("Connection pool inte initierad (lifespan körde inte)")
    with _pool.connection() as raw:
        con = db.Conn(raw)
        try:
            yield con
        finally:
            con.close_cursor()
```

Inga endpoints behöver ändras — alla 15 anropsställen använder redan
`with open_db() as con:` (verifierat 2026-05-21). Om framtida kod anropar
`open_db()` utan `with` bryts den tyst — sök `open_db()` om tveksamt.

- [ ] **Step 4: Verifiera lokalt — starta webappen**

```powershell
$env:DEV_AUTH_BYPASS = "1"
py -m uvicorn webapp.backend.main:app --port 8000
```

I ett andra fönster:

```bash
curl -s http://127.0.0.1:8000/api/health
curl -s "http://127.0.0.1:8000/api/companies" | head -c 200
curl -s "http://127.0.0.1:8000/api/periods"   | head -c 200
```

Förväntat: `/api/health` ger `{"status":"ok"}`; companies/periods ger JSON utan
traceback i uvicorn-loggen. Stoppa servern (Ctrl+C) efteråt.

- [ ] **Step 5: Commit**

```bash
git add requirements.txt db.py webapp/backend/main.py
git commit -m "perf(webapp): connection pooling (psycopg-pool)"
```

---

## Task 8: F0-4 — Cacha SQL-filer + companies/periods

**Files:**
- Modify: `webapp/backend/main.py` — SQL-cache-helper, svars-cache-helper,
  `/api/companies` + `/api/periods`.

- [ ] **Step 1: Lägg till cache-helpers i `main.py`**

I `webapp/backend/main.py`, lägg till efter SQL-sökvägskonstanterna
(`SQL_SUP_BY_CATEGORY = ...`):

```python
# SQL-filer läses en gång och cachas — slipper disk-I/O per request.
_SQL_TEXT_CACHE: dict[Path, str] = {}


def _sql(path: Path) -> str:
    text = _SQL_TEXT_CACHE.get(path)
    if text is None:
        text = path.read_text(encoding="utf-8")
        _SQL_TEXT_CACHE[path] = text
    return text


# Kort TTL-cache för långsamt föränderlig data (companies/periods ändras bara
# när ny data laddas, månadsvis).
RESPONSE_CACHE_TTL = 300.0  # sekunder
_RESPONSE_CACHE: dict[str, tuple[float, object]] = {}


def _cached(key: str, producer):
    """Returnera cachat värde om < RESPONSE_CACHE_TTL gammalt, annars producera."""
    import time as _t
    now = _t.time()
    hit = _RESPONSE_CACHE.get(key)
    if hit is not None and now - hit[0] < RESPONSE_CACHE_TTL:
        return hit[1]
    value = producer()
    _RESPONSE_CACHE[key] = (now, value)
    return value
```

- [ ] **Step 2: Byt alla `.read_text(...)`-anrop på SQL-filer mot `_sql(...)`**

I `main.py`, ersätt varje förekomst av mönstret `SQL_*.read_text(encoding="utf-8")`
med `_sql(SQL_*)`. Förekomsterna (sökväg → endpoint):

- `SQL_PATH` i `pnl_report`
- `SQL_COVERAGE` i `compare_coverage`
- `SQL_COVERAGE_ACCOUNTS` i `compare_coverage_accounts`
- `SQL_PERSONNEL` i `personnel_summary`
- `SQL_PIVOT` i `report_pivot`
- `SQL_SUP_BY_SUPPLIER` i `suppliers_by_supplier`
- `SQL_SUP_BY_CATEGORY` i `suppliers_by_category`

Exempel — i `pnl_report`:

```python
    # före:  sql = SQL_PATH.read_text(encoding="utf-8")
    sql = _sql(SQL_PATH)
```

- [ ] **Step 3: Cacha `/api/companies`**

Ersätt `list_companies`-funktionens kropp med en cachad variant:

```python
@app.get("/api/companies")
async def list_companies():
    """Bolag som har P&L-data i någon period (filtrerade på consolidated)."""
    def _produce():
        with open_db() as con:
            rows = con.fetch_dicts(
                """
                SELECT c.company_id, c.name, c.country, c.currency,
                       COUNT(DISTINCT fb.period) AS n_periods,
                       MAX(fb.period) AS latest_period
                FROM dim_company c
                JOIN fact_balances fb ON fb.company_id = c.company_id
                WHERE COALESCE(c.kind, '') != 'consolidated'
                GROUP BY c.company_id, c.name, c.country, c.currency
                HAVING COUNT(DISTINCT fb.period) > 0
                ORDER BY c.country, c.company_id
                """
            )
        return {"companies": rows}
    return _cached("companies", _produce)
```

- [ ] **Step 4: Cacha `/api/periods`**

Ersätt `list_periods`-funktionens kropp:

```python
@app.get("/api/periods")
async def list_periods(company_id: int | None = Query(None)):
    """Alla perioder med data, eller filtrerat per bolag."""
    def _produce():
        with open_db() as con:
            if company_id is None:
                rows = con.fetch_dicts(
                    """SELECT period, COUNT(DISTINCT company_id) AS n_companies
                       FROM fact_balances GROUP BY period ORDER BY period DESC"""
                )
            else:
                rows = con.fetch_dicts(
                    """SELECT period FROM fact_balances WHERE company_id = %s
                       GROUP BY period ORDER BY period DESC""",
                    [company_id],
                )
        return {"periods": rows}
    return _cached(f"periods:{company_id}", _produce)
```

- [ ] **Step 5: Verifiera lokalt**

```powershell
$env:DEV_AUTH_BYPASS = "1"
py -m uvicorn webapp.backend.main:app --port 8000
```

```bash
curl -s "http://127.0.0.1:8000/api/companies" | head -c 200
curl -s "http://127.0.0.1:8000/api/report/pnl?company_id=5&period=202604" | head -c 200
curl -s "http://127.0.0.1:8000/api/compare/coverage" | head -c 200
```

Förväntat: JSON utan traceback. Andra anropet av `/api/companies` ska synas
momentant (cache-träff). Stoppa servern.

- [ ] **Step 6: Commit**

```bash
git add webapp/backend/main.py
git commit -m "perf(webapp): cacha SQL-filer i minnet + TTL-cache för companies/periods"
```

---

## Task 9: F0-7 — GZip-komprimering av API-svar

**Files:**
- Modify: `webapp/backend/main.py` — registrera `GZipMiddleware`.

- [ ] **Step 1: Lägg till GZip-middleware**

I `webapp/backend/main.py`, lägg till importen vid de andra middleware-
importerna:

```python
from fastapi.middleware.gzip import GZipMiddleware
```

Lägg till middleware-registreringen direkt efter `app = FastAPI(...)` och före
`CORSMiddleware`-registreringen:

```python
# Komprimera stora JSON-svar (pivot/täckning kan vara hundratals kB).
app.add_middleware(GZipMiddleware, minimum_size=1000)
```

- [ ] **Step 2: Verifiera lokalt att svar komprimeras**

```powershell
$env:DEV_AUTH_BYPASS = "1"
py -m uvicorn webapp.backend.main:app --port 8000
```

```bash
curl -s -H "Accept-Encoding: gzip" -D - -o NUL "http://127.0.0.1:8000/api/companies" | findstr /i "content-encoding"
```

Förväntat: `content-encoding: gzip` i svarsheadern. Stoppa servern.

- [ ] **Step 3: Commit**

```bash
git add webapp/backend/main.py
git commit -m "perf(webapp): gzip-komprimering av API-svar"
```

---

## Task 10: Deploy, verifiering och eftermätning

**Files:** inga kodändringar.

- [ ] **Step 1: Pusha grenen och öppna PR mot `main`**

```bash
git push -u origin perf/fas-0-optimering
gh pr create --base main --head perf/fas-0-optimering \
  --title "perf: Fas 0 prestanda-optimering (MCP + GUI)" \
  --body "Genomför Fas 0 ur docs/prestanda-optimering.md: connection pooling, describe_schema approx-count + cache, period-index, SQL-/svars-cache, GZip. Always On, health check och pg_stat_statements är redan satta direkt på Azure (Task 2-3)."
```

- [ ] **Step 2: Merga PR:en — deploy-workflowsen triggas**

Merge av PR:en till `main` triggar `deploy.yml` (webapp) och `deploy-mcp.yml`
(MCP). Följ körningarna:

```bash
gh run list --limit 5
```

Vänta tills båda är gröna.

- [ ] **Step 3: Verifiera produktion**

```bash
curl -s https://app-finauto-mcp-6427.azurewebsites.net/healthz
curl -s https://app-finauto-6427.azurewebsites.net/api/health
curl -s -H "Accept-Encoding: gzip" -D - -o NUL https://app-finauto-6427.azurewebsites.net/api/health | findstr /i "content-encoding"
```

Förväntat: `ok`, `{"status":"ok"}`, och `content-encoding: gzip`.

- [ ] **Step 4: Verifiera `max_connections`-marginalen**

```bash
py -c "import os, psycopg; c=psycopg.connect(os.environ['DATABASE_URL']); print('max_connections =', c.execute('SHOW max_connections').fetchone()[0]); c.close()"
```

Om värdet är lågt (< ~50) — höj via Azure-server-parametern:

```bash
az postgres flexible-server parameter set -g rg-finauto-6427 -s psql-finauto-6427 \
    --name max_connections --value 100
az postgres flexible-server restart -g rg-finauto-6427 -n psql-finauto-6427
```

- [ ] **Step 5: Eftermät och jämför mot baslinjen från Task 1**

Klocka om samma tre åtgärder som i Task 1 Step 3:
1. Första frågan i en ny Claude-konversation — cold start ska vara borta.
2. GUI cold load — ska vara borta.
3. P&L + täckningssidan varma — ska vara snabbare.

Anteckna före/efter. Acceptanskriterier finns i `docs/prestanda-optimering.md`
avsnitt 2. Om "segt sen" kvarstår på tunga frågor → gå vidare till Fas 1.

- [ ] **Step 6: Granska de dyraste frågorna (pg_stat_statements)**

Efter ett par dagars drift:

```bash
py -c "import os, psycopg; c=psycopg.connect(os.environ['DATABASE_URL']); cur=c.cursor(); cur.execute('SELECT round(mean_exec_time::numeric,1) ms, calls, left(query,80) q FROM pg_stat_statements ORDER BY mean_exec_time DESC LIMIT 15'); [print(r) for r in cur.fetchall()]; c.close()"
```

Använd resultatet för att prioritera Fas 1/Fas 2.

---

## Self-review-noteringar

- **Spec-täckning:** F0-1…F0-7 ur `docs/prestanda-optimering.md` motsvaras av
  Task 2, 3 (×2), 4, 5, 6, 7, 8, 9. Mätning (avsnitt 7) = Task 1 + Task 10.
- **Känd avgränsning (medvetet utanför Fas 0):** webappens endpoints är
  `async def` men gör synkrona DB-anrop. `psycopg-pool`s `ConnectionPool` är
  synkron och blockerar event-loopen kortvarigt vid varje lån — samma mönster
  som idag (`db.connect` blockerar redan). Poolen *minskar* blockeringen.
  Full `AsyncConnectionPool` + async-endpoints är en större refaktor — Fas 2.
- **Merge-yta:** `db.py` rörs av både denna plan (Task 4, 7) och den parallella
  SIE_VER-grenen. Ändringarna är på olika rader — trivial merge.
