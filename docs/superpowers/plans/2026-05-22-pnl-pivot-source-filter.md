# Källfilter-dropdown i P&L-pivoten — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lägg till en dropdown med tre kryssrutor (Baskälla / MAN / IMP_ADJ) i P&L-pivotens filterpanel så användaren kan styra vilka källager som summeras in i utfallet.

**Architecture:** `report_pivot.sql` görs additiv (speglar `report_pnl.sql`) och får ett lager-filter via tre booleska bind-parametrar. `/api/report/pivot` exponerar en `source_layers`-query-param. `PnlPivot.tsx` får en dropdown som speglar befintliga "Kolumner"-pickern och driver actuals-anropet.

**Tech Stack:** PostgreSQL (psycopg3, positionsbundna `%s`), FastAPI (Python), React 19 + TypeScript + Vite + Tailwind.

**Spec:** `docs/superpowers/specs/2026-05-22-pnl-pivot-source-filter-design.md`

**Verifieringsgater (projektet saknar testramverk):**
- SQL: `py scripts/smoke_test_sql.py` (kräver `DATABASE_URL`).
- Frontend: `npm run build` i `webapp/frontend` (kör `tsc -b` typkoll + vite build).
- Korrekthet: jämför `/api/report/pivot` mot `/api/report/pnl` i Task 5.

`DATABASE_URL` sätts (PowerShell) med:
```powershell
$env:DATABASE_URL = (az keyvault secret show --vault-name kv-finauto-6427 --name database-url --query value -o tsv)
```

---

## Task 1: Gör `report_pivot.sql` additiv + lager-filter (med smoke-test)

**Files:**
- Modify: `scripts/smoke_test_sql.py`
- Modify: `webapp/backend/sql/report_pivot.sql`

- [ ] **Step 1: Skriv det failande smoke-testet**

I `scripts/smoke_test_sql.py`, lägg till en ny funktion direkt efter `test_coverage_accounts()` (efter rad 89):

```python
def test_report_pivot() -> bool:
    """report_pivot.sql körs som main.py:report_pivot() — {bucket_values} substitueras.

    Param-ordning efter substitution: bucket(3) + company_ids + source_kind
    + include_base + include_man + include_imp_adj + scenario + report_currency.
    """
    body = (SQL_DIR / "report_pivot.sql").read_text(encoding="utf-8")
    # main.py bygger "VALUES (%s,%s,%s), ..." en gång per bucket — här: en bucket.
    body = body.replace("{bucket_values}", "VALUES (%s, %s, %s)")
    params = (
        "2026-03", "202603", "202603",   # bucket: key, start_period, end_period
        [72],                            # company_ids (INTEGER[]) — bolag 72 Dala Lås
        None,                            # source_kind (NULL = auto)
        True, True, True,                # include_base / include_man / include_imp_adj
        "A",                             # scenario
        "LOCAL",                         # report_currency
    )
    return _run("report_pivot.sql", body, params)
```

Lägg sedan in anropet i `results`-listan i `main()` (nuvarande rad 111–114):

```python
    results = [
        test_compare_coverage(),
        test_coverage_accounts(),
        test_report_pivot(),
    ]
```

- [ ] **Step 2: Kör smoke-testet — verifiera att report_pivot FAILar**

Run: `py scripts/smoke_test_sql.py`
Expected: `compare_coverage.sql` och `coverage_accounts.sql` OK, men
`report_pivot.sql` → `FAIL (programming)` — nuvarande SQL har 7 placeholders men testet skickar 10 parametrar. Sista raden: `1 av 3 tests failade.`

- [ ] **Step 3: Uppdatera header-kommentaren i `report_pivot.sql`**

Ersätt rad 9–14 (blocket `-- Bind-parametrar ...`):

```sql
-- Bind-parametrar (i ordning, EFTER {bucket_values}-substitutionen):
--   - alla bucket-värden (3 per bucket: key, start_period, end_period)
--   - company_ids       : INTEGER[]
--   - scenario                     : TEXT ('A' eller 'B')
--   - report_currency              : TEXT ('SEK' eller 'LOCAL') — andra → LOCAL
--   - source_kind override         : TEXT eller NULL (auto via prio per land)
```

Med:

```sql
-- Bind-parametrar (i ordning, EFTER {bucket_values}-substitutionen):
--   - alla bucket-värden (3 per bucket: key, start_period, end_period)
--   - company_ids       : INTEGER[]
--   - source_kind       : TEXT eller NULL (override; NULL = auto via prio per land)
--   - include_base      : BOOLEAN — summera in bas-källan (best_source)
--   - include_man       : BOOLEAN — summera in MAN-justeringslagret
--   - include_imp_adj   : BOOLEAN — summera in IMP_ADJ-justeringslagret
--   - scenario          : TEXT ('A' eller 'B')
--   - report_currency   : TEXT ('SEK' eller 'LOCAL') — andra → LOCAL
--
-- Utfall = vald bas-källa (best_source) + additivt MAN + IMP_ADJ ovanpå.
-- best_source väljer ALDRIG MAN/IMP_ADJ — de är additiva lager (se raw_balances).
```

- [ ] **Step 4: Ta bort `IMP_ADJ` ur `best_source`-prioriteten**

`IMP_ADJ` ska aldrig väljas som bas-källa. I `best_source`-CTE:n finns raden i fyra grenar (Sweden, Norway, CA, ELSE). Ta bort alla fyra.

Sweden/Norway/CA har identisk rad — kör en Edit med `replace_all: true`:

Replace (förekommer 3 ggr):
```sql
                    WHEN MAX(CASE WHEN fb.source_kind = 'IMP'        THEN 1 ELSE 0 END) = 1 THEN 'IMP'
                    WHEN MAX(CASE WHEN fb.source_kind = 'IMP_ADJ'    THEN 1 ELSE 0 END) = 1 THEN 'IMP_ADJ'
                    ELSE NULL
```
With:
```sql
                    WHEN MAX(CASE WHEN fb.source_kind = 'IMP'        THEN 1 ELSE 0 END) = 1 THEN 'IMP'
                    ELSE NULL
```

ELSE-grenen (Finland/Denmark/Germany/CENTR) har annan indentering — separat Edit:

Replace:
```sql
                    WHEN MAX(CASE WHEN fb.source_kind = 'IMP'     THEN 1 ELSE 0 END) = 1 THEN 'IMP'
                    WHEN MAX(CASE WHEN fb.source_kind = 'IMP_ADJ' THEN 1 ELSE 0 END) = 1 THEN 'IMP_ADJ'
                    ELSE NULL
```
With:
```sql
                    WHEN MAX(CASE WHEN fb.source_kind = 'IMP'     THEN 1 ELSE 0 END) = 1 THEN 'IMP'
                    ELSE NULL
```

- [ ] **Step 5: Gör `raw_balances` additiv + lager-filtrerad + behåll `period_type`**

Ersätt hela `raw_balances`-CTE:n (nuvarande rad 105–121, kommentar + CTE):

```sql
-- 3. Råa balances för valt scenario, summerade per (bolag, period, konto).
--    P-koder normaliseras till SIE-konvention (negat).
raw_balances AS (
    SELECT
        fb.company_id, fb.period, fb.account_code,
        MAX(fb.account_name) AS account_name,
        MAX(fb.period_type)  AS period_type,
        SUM(fb.amount * CASE WHEN fb.account_code LIKE 'P_%%' THEN -1 ELSE 1 END) AS amount
    FROM fact_balances fb
    JOIN best_source bs
        ON bs.company_id  = fb.company_id
       AND bs.period      = fb.period
       AND bs.source_kind = fb.source_kind
    JOIN months_with_prev mw ON mw.period = fb.period
    WHERE fb.scenario = %s
    GROUP BY fb.company_id, fb.period, fb.account_code
),
```

Med:

```sql
-- 3. Råa balances för valt scenario, summerade per (bolag, period, konto, period_type).
--    P-koder normaliseras till SIE-konvention (negat).
--
--    Additivt: bas-källan (best_source) ELLER ett justeringslager (MAN/IMP_ADJ).
--    Lager-filtret — tre booleska parametrar — styr vilka lager som tas med.
--    OR i join-villkoret avgör bara OM en fb-rad joinar, inte hur många gånger.
--
--    period_type ligger i GROUP BY: en YTD-bas-rad (SE/NO) och en monthly
--    MAN/IMP_ADJ-rad för samma konto hålls isär (olika periodsemantik) och
--    summeras först i bucket_amounts. Utan detta skulle de slås ihop och
--    monthly-justeringen felaktigt YTD-diffas i month_amounts.
raw_balances AS (
    SELECT
        fb.company_id, fb.period, fb.account_code, fb.period_type,
        MAX(fb.account_name) AS account_name,
        SUM(fb.amount * CASE WHEN fb.account_code LIKE 'P_%%' THEN -1 ELSE 1 END) AS amount
    FROM fact_balances fb
    JOIN best_source bs
        ON bs.company_id  = fb.company_id
       AND bs.period      = fb.period
       AND (
            (%s::boolean AND bs.source_kind = fb.source_kind)
         OR (%s::boolean AND fb.source_kind = 'MAN')
         OR (%s::boolean AND fb.source_kind = 'IMP_ADJ')
       )
    JOIN months_with_prev mw ON mw.period = fb.period
    WHERE fb.scenario = %s
    GROUP BY fb.company_id, fb.period, fb.account_code, fb.period_type
),
```

`month_amounts` (nästa CTE) refererar redan `cur.period_type` per rad och behöver ingen ändring — `period_type` är nu en äkta grupperingskolumn istället för `MAX(...)`.

- [ ] **Step 6: Kör smoke-testet — verifiera att report_pivot PASSar**

Run: `py scripts/smoke_test_sql.py`
Expected: alla tre OK. Sista raden: `All 3 tests OK.` Exit-kod 0.

- [ ] **Step 7: Commit**

```bash
git add scripts/smoke_test_sql.py webapp/backend/sql/report_pivot.sql
git commit -m "fix(report_pivot): additiv MAN/IMP_ADJ + lager-filter-parametrar"
```

---

## Task 2: Backend — `source_layers`-param på `/api/report/pivot`

**Files:**
- Modify: `webapp/backend/main.py` (`report_pivot`, ca rad 657–724)

- [ ] **Step 1: Lägg till query-parametern**

I `report_pivot`-signaturen, lägg en ny rad direkt efter `source_kind`-parametern (nuvarande rad 668):

```python
    source_kind: str | None = Query(None, description="Tvinga viss källa (annars auto per land)"),
    source_layers: str = Query(
        "base,man,imp_adj",
        description="Komma-sep lager att summera: base,man,imp_adj. Default alla.",
    ),
```

- [ ] **Step 2: Parsa lagren och bygg om `params`**

Ersätt `params`-tilldelningen (nuvarande rad 721–724):

```python
        params = (
            bucket_params
            + [company_ids_list, source_kind, scenario, report_currency]
        )
```

Med:

```python
        # Lager-filter: 'base' = bas-källan (best_source), 'man'/'imp_adj' =
        # additiva justeringslager. Default alla tre. Okända tokens ignoreras.
        layers = {s.strip().lower() for s in source_layers.split(",") if s.strip()}
        include_base    = "base"    in layers
        include_man     = "man"     in layers
        include_imp_adj = "imp_adj" in layers
        params = (
            bucket_params
            + [company_ids_list, source_kind,
               include_base, include_man, include_imp_adj,
               scenario, report_currency]
        )
```

- [ ] **Step 3: Verifiera att Python-filen parsar**

Run: `py -m py_compile webapp/backend/main.py`
Expected: ingen output, exit-kod 0 (syntaxfel skulle skrivas till stderr).

- [ ] **Step 4: Commit**

```bash
git add webapp/backend/main.py
git commit -m "feat(api): source_layers-param på /api/report/pivot"
```

---

## Task 3: Frontend API-klient — `source_layers` i `PivotQuery`

**Files:**
- Modify: `webapp/frontend/src/api.ts`

- [ ] **Step 1: Lägg till `SourceLayer`-typen**

Direkt efter raden `export type ReportCurrency = "SEK" | "LOCAL";` (nuvarande rad 250):

```ts
export type SourceLayer = "base" | "man" | "imp_adj";
```

- [ ] **Step 2: Lägg fältet i `PivotQuery`**

I `PivotQuery`-interfacet, lägg en rad direkt efter `source_kind?: string;` (nuvarande rad 315):

```ts
  source_kind?: string;
  source_layers?: SourceLayer[];
```

- [ ] **Step 3: Skicka med parametern i `fetchPivot`**

I `fetchPivot`, direkt efter raden `if (q.source_kind) params.append("source_kind", q.source_kind);` (nuvarande rad 481):

```ts
  if (q.source_kind) params.append("source_kind", q.source_kind);
  if (q.source_layers && q.source_layers.length)
    params.append("source_layers", q.source_layers.join(","));
```

- [ ] **Step 4: Typkolla**

Run: `cd webapp/frontend; npm run build`
Expected: `tsc -b` och `vite build` utan fel.

- [ ] **Step 5: Commit**

```bash
git add webapp/frontend/src/api.ts
git commit -m "feat(api-client): source_layers i PivotQuery"
```

---

## Task 4: Frontend UI — källfilter-dropdown i `PnlPivot`

**Files:**
- Modify: `webapp/frontend/src/components/PnlPivot.tsx`

- [ ] **Step 1: Utöka importerna**

Ersätt rad 2 (lucide-import):

```tsx
import { ChevronDown, ChevronRight, Eye, EyeOff } from "lucide-react";
```

Med:

```tsx
import { ChevronDown, ChevronRight, Eye, EyeOff, Layers } from "lucide-react";
```

Ersätt API-import-blocket (nuvarande rad 3–6):

```tsx
import {
  Company, Granularity, PivotKpi, PivotReport, PivotRow, ReportCurrency,
  fetchCompanies, fetchPeriods, fetchPivot,
} from "../api";
```

Med:

```tsx
import {
  Company, Granularity, PivotKpi, PivotReport, PivotRow, ReportCurrency,
  SourceLayer, fetchCompanies, fetchPeriods, fetchPivot,
} from "../api";
```

- [ ] **Step 2: Lägg till state för lager-filtret**

Direkt efter raden `const [showColumnPicker, setShowColumnPicker] = useState<boolean>(false);` (nuvarande rad 199):

```tsx
  const [showColumnPicker, setShowColumnPicker] = useState<boolean>(false);
  // Lager-filter: vilka källager som summeras in i utfallet (scenario A).
  const [sourceLayers, setSourceLayers] = useState<{ base: boolean; man: boolean; imp_adj: boolean }>(
    { base: true, man: true, imp_adj: true },
  );
  const [showSourcePicker, setShowSourcePicker] = useState<boolean>(false);
```

- [ ] **Step 3: Härled lager-listan**

Direkt efter `sourceLayers`/`showSourcePicker`-staten (från Step 2), lägg ett memo:

```tsx
  const selectedLayers = useMemo<SourceLayer[]>(() => {
    const out: SourceLayer[] = [];
    if (sourceLayers.base) out.push("base");
    if (sourceLayers.man) out.push("man");
    if (sourceLayers.imp_adj) out.push("imp_adj");
    return out;
  }, [sourceLayers]);
```

- [ ] **Step 4: Skicka lagren till fetch-anropen**

Ersätt actuals/budget-anropen i hämtnings-`useEffect`:en (nuvarande rad 242–251):

```tsx
    const actualP = fetchPivot({ ...baseQuery, scenario: "A" });
    const budgetP = includeBudget
      ? fetchPivot({
          ...baseQuery,
          // För budget: bara YTD-bucket räcker normalt; vi kör samma granularity
          // som utfall så användaren får jämförbara kolumner. Källa MAN, scenario B.
          scenario:    "B",
          source_kind: "MAN",
        })
      : null;
```

Med:

```tsx
    const actualP = fetchPivot({ ...baseQuery, scenario: "A", source_layers: selectedLayers });
    const budgetP = includeBudget
      ? fetchPivot({
          ...baseQuery,
          // För budget: bara YTD-bucket räcker normalt; vi kör samma granularity
          // som utfall så användaren får jämförbara kolumner. Källa MAN, scenario B.
          scenario:    "B",
          source_kind: "MAN",
          // Budget = scenario B med forcerad source_kind=MAN; lager-filtret styr
          // bara scenario A:s utfall. ["base"] explicit så att en eventuell
          // scenario-B IMP_ADJ inte smyger in i budgetkolumnen.
          source_layers: ["base"],
        })
      : null;
```

- [ ] **Step 5: Lägg `selectedLayers` i useEffect-deps**

Ersätt deps-arrayen för hämtnings-`useEffect`:en (nuvarande rad 266–267):

```tsx
  }, [scope, periodFrom, periodTo, granularity, reportCurrency,
      includeLtm, includeYtd, includeBudget]);
```

Med:

```tsx
  }, [scope, periodFrom, periodTo, granularity, reportCurrency,
      includeLtm, includeYtd, includeBudget, selectedLayers]);
```

- [ ] **Step 6: Rendera dropdownen**

I filterpanelen, direkt FÖRE blocket `{/* Kolumn-visibility */}` (nuvarande rad 462), lägg in:

```tsx
        {/* Källor — lager-filter (bas / MAN / IMP_ADJ) */}
        <div className="relative">
          <button
            onClick={() => setShowSourcePicker((v) => !v)}
            className="px-3 py-1.5 rounded-md border border-border bg-surface text-fg-muted text-xs hover:bg-elevated inline-flex items-center gap-1"
            title="Välj vilka källager som summeras in i utfallet"
          >
            <Layers size={12} aria-hidden />
            Källor ({selectedLayers.length}/3)
          </button>
          {showSourcePicker && (
            <div className="absolute right-0 mt-1 z-20 bg-surface border border-border rounded-md shadow-lg p-2 min-w-[12rem] text-xs">
              {([
                { key: "base",    label: "Baskälla (auto)" },
                { key: "man",     label: "MAN-justeringar" },
                { key: "imp_adj", label: "IMP_ADJ-justeringar" },
              ] as { key: "base" | "man" | "imp_adj"; label: string }[]).map((l) => (
                <label
                  key={l.key}
                  className="flex items-center gap-2 px-2 py-1 hover:bg-elevated rounded cursor-pointer"
                >
                  <input
                    type="checkbox"
                    checked={sourceLayers[l.key]}
                    onChange={() =>
                      setSourceLayers((prev) => ({ ...prev, [l.key]: !prev[l.key] }))
                    }
                  />
                  <span>{l.label}</span>
                </label>
              ))}
            </div>
          )}
        </div>

```

- [ ] **Step 7: Typkolla + bygg**

Run: `cd webapp/frontend; npm run build`
Expected: `tsc -b` och `vite build` utan fel.

- [ ] **Step 8: Commit**

```bash
git add webapp/frontend/src/components/PnlPivot.tsx
git commit -m "feat(pnl-pivot): källfilter-dropdown (bas/MAN/IMP_ADJ)"
```

---

## Task 5: End-to-end-verifiering

**Files:** inga (verifiering).

- [ ] **Step 1: SQL-smoke-test (regression)**

Run (med `DATABASE_URL` satt): `py scripts/smoke_test_sql.py`
Expected: `All 3 tests OK.`

- [ ] **Step 2: Frontend-build (regression)**

Run: `cd webapp/frontend; npm run build`
Expected: utan fel.

- [ ] **Step 3: Hitta testbolag med justeringsdata**

Kör mot warehouse (MCP `query_sql` eller psql) — hitta ett SE-bolag och ett FI-bolag som har MAN- eller IMP_ADJ-rader i en period:

```sql
SELECT c.country, fb.company_id, fb.period, fb.source_kind, COUNT(*) AS rows
FROM fact_balances fb
JOIN dim_company c ON c.company_id = fb.company_id
WHERE fb.source_kind IN ('MAN', 'IMP_ADJ')
  AND fb.scenario = 'A'
  AND c.country IN ('Sweden', 'Finland')
GROUP BY c.country, fb.company_id, fb.period, fb.source_kind
ORDER BY c.country, rows DESC
LIMIT 10;
```

Notera ett `(company_id, period)` för Sweden och ett för Finland till nästa steg.

- [ ] **Step 4: Starta backend lokalt**

I repo-roten (PowerShell), med `DATABASE_URL` satt:

```powershell
$env:DEV_AUTH_BYPASS = "1"
py -m uvicorn webapp.backend.main:app --port 8000
```

Låt servern köra i ett eget fönster/bakgrund.

- [ ] **Step 5: Korrekthetskontroll — pivot vs pnl**

För det SE-bolag och den period du noterade i Step 3 (exempel `company_id=X`, `period=YYYYMM`), jämför de två endpointsen. `report_pnl` ger `amount_month` per `account_id`; `report_pivot` med en månads-bucket ger samma cell:

```powershell
# report_pnl — facit
Invoke-RestMethod "http://localhost:8000/api/report/pnl?company_id=X&period=YYYYMM" |
  Select-Object -ExpandProperty rows |
  Where-Object { -not $_.is_aggregated } |
  Select-Object account_id, amount_month | Sort-Object account_id

# report_pivot — alla lager på, en månads-bucket
Invoke-RestMethod "http://localhost:8000/api/report/pivot?company_ids=X&period_from=YYYYMM&period_to=YYYYMM&granularity=month&source_layers=base,man,imp_adj" |
  Select-Object -ExpandProperty rows |
  Where-Object { -not $_.is_aggregated } |
  Select-Object account_id, by_company | Sort-Object account_id
```

Expected: per `account_id` ska pivotens enda bucket-cell (värdet i `by_company`) vara lika med `report_pnl`:s `amount_month`. Upprepa för FI-bolaget. Avvikelse = den additiva fixen är fel — stanna och felsök innan merge.

- [ ] **Step 6: Lager-toggle-kontroll**

Jämför summan med och utan justeringslagren:

```powershell
Invoke-RestMethod "http://localhost:8000/api/report/pivot?company_ids=X&period_from=YYYYMM&period_to=YYYYMM&granularity=month&source_layers=base"
Invoke-RestMethod "http://localhost:8000/api/report/pivot?company_ids=X&period_from=YYYYMM&period_to=YYYYMM&granularity=month&source_layers=base,man,imp_adj"
```

Expected: de två svaren skiljer sig (justeringslagren bidrar med belopp); `source_layers=base` ger bas-källan ensam.

- [ ] **Step 7: Manuell GUI-koll**

Starta frontend: `cd webapp/frontend; npm run dev`. Öppna appen, gå till P&L-fliken:
- "Källor (3/3)"-knappen syns i filterraden, före "Kolumner".
- Klick öppnar dropdown med tre kryssrutor, alla ikryssade.
- Avbockning av en ruta laddar om rapporten (knapptexten blir t.ex. "Källor (2/3)") och beloppen ändras.

- [ ] **Step 8: Inget att committa** — verifieringssteg. Om alla kontroller passerar är featuren klar.

---

## Self-review-noteringar

- **Spec-täckning:** SQL additiv + period_type-fix + lager-filter (Task 1) · backend-param (Task 2) · api.ts (Task 3) · PnlPivot-dropdown + budget-`["base"]` (Task 4) · acceptanskriterier 1–4 (Task 5). Alla spec-sektioner täckta.
- **Param-ordning** är identisk i `report_pivot.sql` (Task 1 Step 5), header-kommentaren (Task 1 Step 3) och `main.py` (Task 2 Step 2): `… company_ids, source_kind, include_base, include_man, include_imp_adj, scenario, report_currency`.
- **Typkonsistens:** `SourceLayer` definieras i Task 3 Step 1 och används i Task 3 (`PivotQuery`) och Task 4 (`selectedLayers`, importen). `sourceLayers`-objektets nycklar (`base`/`man`/`imp_adj`) är konsekventa mellan state, memo och dropdown.
