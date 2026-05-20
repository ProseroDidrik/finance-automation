# Coverage Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-konto-mismatch-klassificering för SIE/SAFT i `compare_coverage` + ny drilldown-drawer som visar konto-diff från klick på rad i täckningssidans drilldown-tabell.

**Architecture:** Två sammanhängande ändringar mot samma SQL-fundament. En gemensam `account_diff`-CTE (UNION ALL av YTD-gren för SIE/SAFT + monthly-gren för IMP/MAN/IMP_ADJ) konsumeras dels av matris-statusen (via `EXISTS` per cell), dels av en ny drilldown-endpoint. Frontend lägger till en sidodrawer som öppnas på klick i den befintliga drill-tabellen.

**Tech Stack:** FastAPI + psycopg3 (backend), Postgres på Azure Flexible Server, React + TypeScript + Vite + Tailwind (frontend), lucide-react för ikoner. Inga tester finns i kodbasen — verifiering sker manuellt via `psql` / `curl` / webbläsare (matchar spec §6).

**Spec:** `docs/superpowers/specs/2026-05-17-coverage-quality-design.md`

---

## File Structure

| Fil | Åtgärd | Ansvar |
|---|---|---|
| `webapp/backend/sql/coverage_accounts.sql` | **Ny** | SQL för per-konto-diff (en parametriserad query per drilldown-call) |
| `webapp/backend/sql/compare_coverage.sql` | Modifiera | Lägg till `account_diff`-CTE + byt mismatch-CASE till `EXISTS`-test |
| `webapp/backend/main.py` | Modifiera (~rad 306 + nytt block) | Lägg till `GET /api/compare/coverage/accounts` |
| `webapp/frontend/src/api.ts` | Modifiera | Nya typer `CoverageAccountRow`, `CoverageAccountsReport` + `fetchCoverageAccounts()` |
| `webapp/frontend/src/components/CoverageAccountsDrawer.tsx` | **Ny** | Drawer-komponent (header, summary-chips, sorterbar diff-tabell, escape-stäng) |
| `webapp/frontend/src/components/CoverageReport.tsx` | Modifiera | Gör drill-tabellens rader klickbara + rendera `<CoverageAccountsDrawer>` |

**Ordningsstrategi:** Bygg drilldown end-to-end först (additivt, ändrar inget existerande beteende). Spara matris-uppdateringen sist eftersom den ändrar `mismatch`-semantiken — då kan vi rulla tillbaka den isolerat om snapshot-jämförelsen ser tokig ut.

---

## Domänkontext (läs först om okänd kodbas)

- **`backup_from_mercur`** är facit-tabellen (Mercur-export). `account_name` är **alltid NULL** där — vi får alltid kontonamnet från `fact_balances`.
- **`fact_balances`** är vår laddade data. `source_kind` ∈ {`SIE`, `SIE_PSALDO`, `SAFT`, `IMP`, `MAN`, `IMP_ADJ`, `IB`}. För SE väljer existerande SQL `SIE` framför `SIE_PSALDO` via `sie_pick`-CTE; samma mönster återanvänds.
- **`period_type`:** `SIE`/`SAFT` lagras som YTD (ackumulerat från 1 jan), övriga som monthly. Backup är alltid monthly. Detta är hela motiveringen till `account_diff`-CTE:n: vi YTD-kumulerar facit-sidan innan jämförelse mot YTD-fact för SIE/SAFT.
- **`scenario='A'`** = utfall, default för alla matris-/drilldown-frågor.
- **`HIDDEN_SOURCE_KINDS = ["MAN", "IMP_ADJ"]`** i `CoverageReport.tsx`: dessa filtreras bort från matrisen. I praktiken triggas drilldown därför bara på `IMP`/`SIE`/`SAFT`, men endpoint accepterar alla för framtida bruk.
- **Postgres-syntax**, inte DuckDB: `LEFT(period, 4)` (period är TEXT), `to_char(date, 'YYYYMM')`, `STRING_AGG`. Inget `QUALIFY`, inget `FILTER`-suffix om vi kan undvika det.
- **DB-anslutning:** `from db import connect; con = connect(read_only=True); con.fetch_dicts(sql, params)` — psycopg3 wrappad. Params skickas som `list`/`tuple` med `%s`-placeholders.
- **Local dev:** `DATABASE_URL` hämtas från Azure Key Vault; backend startas med `py -m uvicorn webapp.backend.main:app --reload --port 8000` från repo-roten. Frontend: `cd webapp/frontend && npm run dev` (Vite på port 5173, proxy:ar `/api` till 8000).

---

## Task 1: Prototypa `account_diff`-CTE mot live DB

**Files:**
- Skapa: `_scratch/account_diff_prototype.sql` (utanför webapp; .gitignored — engångsverifiering)

Mål: bevisa att CTE:n returnerar förväntade rader för ett känt bolag/period innan vi commitar något. Inga produktionsfiler ändras.

- [ ] **Step 1: Skriv prototype-SQL till disk**

Skapa filen `_scratch/account_diff_prototype.sql`:

```sql
-- Prototype: account_diff CTE för coverage-quality-projektet.
-- Verifiera mot bolag 134 / 202604 / IMP innan vi flyttar in i compare_coverage.sql.

WITH
sie_pick AS (
    SELECT DISTINCT company_id, period, scenario,
           FIRST_VALUE(source_kind) OVER (
               PARTITION BY company_id, period, scenario
               ORDER BY CASE source_kind WHEN 'SIE' THEN 1
                                          WHEN 'SIE_PSALDO' THEN 2 END
           ) AS picked_kind
    FROM fact_balances
    WHERE source_kind IN ('SIE', 'SIE_PSALDO') AND scenario = 'A'
),
-- Gren A: SIE/SAFT — YTD-kumulera facit per (bolag, år, källa, konto).
account_diff_ytd AS (
    SELECT
        COALESCE(bk.company_id, fk.company_id)     AS company_id,
        COALESCE(bk.period,     fk.period)         AS period,
        COALESCE(bk.source_kind, fk.source_kind)   AS source_kind,
        COALESCE(bk.scenario,   fk.scenario)       AS scenario,
        COALESCE(bk.account_code, fk.account_code) AS account_code,
        fk.account_name,
        bk.facit_amt,
        fk.fact_amt,
        ROUND((COALESCE(bk.facit_amt, 0) - COALESCE(fk.fact_amt, 0))::numeric, 2) AS diff
    FROM (
        SELECT company_id, period, source_kind, scenario, account_code,
               SUM(amount) OVER (
                   PARTITION BY company_id, LEFT(period, 4), source_kind, scenario, account_code
                   ORDER BY period
                   ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
               ) AS facit_amt
        FROM backup_from_mercur
        WHERE scenario = 'A' AND source_kind IN ('SIE', 'SAFT')
    ) bk
    FULL OUTER JOIN (
        -- För SIE: välj picked_kind via sie_pick; för SAFT: rakt av.
        SELECT fb.company_id, fb.period,
               CASE WHEN fb.source_kind IN ('SIE','SIE_PSALDO') THEN 'SIE' ELSE fb.source_kind END AS source_kind,
               fb.scenario, fb.account_code, fb.account_name,
               fb.amount AS fact_amt
        FROM fact_balances fb
        LEFT JOIN sie_pick p
          ON p.company_id = fb.company_id AND p.period = fb.period AND p.scenario = fb.scenario
        WHERE fb.scenario = 'A'
          AND fb.source_kind IN ('SAFT')
        UNION ALL
        SELECT fb.company_id, fb.period, 'SIE' AS source_kind, fb.scenario,
               fb.account_code, fb.account_name, fb.amount AS fact_amt
        FROM fact_balances fb
        JOIN sie_pick p
          ON p.company_id = fb.company_id AND p.period = fb.period
         AND p.scenario   = fb.scenario   AND p.picked_kind = fb.source_kind
        WHERE fb.scenario = 'A'
    ) fk
      ON bk.company_id  = fk.company_id
     AND bk.period      = fk.period
     AND bk.source_kind = fk.source_kind
     AND bk.scenario    = fk.scenario
     AND bk.account_code = fk.account_code
),
-- Gren B: IMP/MAN/IMP_ADJ — monthly rakt av (ingen YTD-kumulering).
account_diff_monthly AS (
    SELECT
        COALESCE(bk.company_id, fk.company_id)     AS company_id,
        COALESCE(bk.period,     fk.period)         AS period,
        COALESCE(bk.source_kind, fk.source_kind)   AS source_kind,
        COALESCE(bk.scenario,   fk.scenario)       AS scenario,
        COALESCE(bk.account_code, fk.account_code) AS account_code,
        fk.account_name,
        bk.amount AS facit_amt,
        fk.amount AS fact_amt,
        ROUND((COALESCE(bk.amount, 0) - COALESCE(fk.amount, 0))::numeric, 2) AS diff
    FROM (
        SELECT company_id, period, source_kind, scenario, account_code, amount
        FROM backup_from_mercur
        WHERE scenario = 'A' AND source_kind IN ('IMP', 'MAN', 'IMP_ADJ')
    ) bk
    FULL OUTER JOIN (
        SELECT company_id, period, source_kind, scenario, account_code, account_name, amount
        FROM fact_balances
        WHERE scenario = 'A' AND source_kind IN ('IMP', 'MAN', 'IMP_ADJ')
    ) fk USING (company_id, period, source_kind, scenario, account_code)
),
account_diff AS (
    SELECT *,
           CASE
               WHEN facit_amt IS NULL THEN 'only_fact'
               WHEN fact_amt  IS NULL THEN 'only_facit'
               WHEN ABS(diff) > GREATEST(1.0, 0.01 * ABS(facit_amt)) THEN 'amount_diff'
               ELSE 'ok'
           END AS status_acc
    FROM (
        SELECT * FROM account_diff_ytd
        UNION ALL
        SELECT * FROM account_diff_monthly
    ) merged
)
SELECT *
FROM account_diff
WHERE company_id = 134 AND period = '202604' AND source_kind = 'IMP'
ORDER BY status_acc, ABS(diff) DESC NULLS LAST, account_code;
```

- [ ] **Step 2: Verifiera anslutning + kör prototype**

```powershell
$env:DATABASE_URL = (az keyvault secret show --vault-name kv-finauto-6427 --name database-url --query value -o tsv)
psql "$env:DATABASE_URL" -f _scratch/account_diff_prototype.sql | head -40
```

Förväntat: rader för bolag 134, period 202604, källa IMP. Kolumner: `company_id, period, source_kind, scenario, account_code, account_name, facit_amt, fact_amt, diff, status_acc`. `status_acc` ∈ `{ok, amount_diff, only_facit, only_fact}`. Sortering: status först, sedan |diff| desc.

- [ ] **Step 3: Sanity-check mot ett känt YTD-fall**

Byt sista WHERE-villkoret till en SE-bolag som finns 202604:

```sql
WHERE company_id = 32 AND period = '202604' AND source_kind = 'SIE'
```

Kör om. Förväntat: facit_amt är YTD-kumulerat (`SUM(jan..apr)`), fact_amt är YTD-rådata från `fact_balances` (SIE är `period_type='ytd'`). För ett ok-bolag ska `|diff| ≤ 1.0` på de flesta konton.

- [ ] **Step 4: Mät query-tid**

```powershell
psql "$env:DATABASE_URL" -c "EXPLAIN ANALYZE $(Get-Content -Raw _scratch/account_diff_prototype.sql | Out-String)"
```

Förväntat: total körtid < 2 s för en enstaka (bolag, period, källa)-filter. Om > 5 s — flagga och optimera (index på `backup_from_mercur(company_id, period, source_kind, scenario, account_code)` kan saknas).

- [ ] **Step 5: Lägg `_scratch/` i .gitignore om saknas**

```powershell
$gi = Get-Content .gitignore -Raw
if ($gi -notmatch '_scratch/') { Add-Content .gitignore '`n_scratch/' }
```

Inget commit i Task 1. Prototype-filen ligger bara i `_scratch/` för referens när Task 2 skrivs.

---

## Task 2: Skapa `coverage_accounts.sql`

**Files:**
- Skapa: `webapp/backend/sql/coverage_accounts.sql`

Mål: parametriserad version av prototype-queryn, redo för endpoint att läsa. Returnerar rader för **en** (bolag, period, källa)-kombination.

- [ ] **Step 1: Skriv SQL-filen**

```sql
-- Per-konto-diff mellan backup_from_mercur (facit) och fact_balances (laddat data)
-- för en specifik (company_id, period, source_kind, scenario='A').
--
-- Används av /api/compare/coverage/accounts som drilldown-data från
-- täckningssidans matris.
--
-- Parametrar ($1..$3):
--   $1 = company_id   (int)
--   $2 = period       (text, YYYYMM)
--   $3 = source_kind  (text, en av: IMP, SIE, SAFT, MAN, IMP_ADJ)
--
-- För SIE/SAFT YTD-kumuleras backup (monthly→YTD) innan jämförelse mot fact (YTD).
-- För IMP/MAN/IMP_ADJ jämförs monthly↔monthly rakt av.
--
-- status_acc:
--   'ok'           |diff| ≤ max(1.0, 1% × |facit_amt|)
--   'amount_diff'  båda finns, |diff| över tröskel
--   'only_facit'   bara backup har raden
--   'only_fact'    bara fact_balances har raden
WITH
sie_pick AS (
    SELECT DISTINCT company_id, period, scenario,
           FIRST_VALUE(source_kind) OVER (
               PARTITION BY company_id, period, scenario
               ORDER BY CASE source_kind WHEN 'SIE' THEN 1
                                          WHEN 'SIE_PSALDO' THEN 2 END
           ) AS picked_kind
    FROM fact_balances
    WHERE source_kind IN ('SIE', 'SIE_PSALDO') AND scenario = 'A'
),
account_diff_ytd AS (
    -- SIE/SAFT-grenen: YTD-kumulera backup per konto, FULL JOIN mot YTD-fact.
    SELECT
        COALESCE(bk.company_id, fk.company_id)     AS company_id,
        COALESCE(bk.period,     fk.period)         AS period,
        COALESCE(bk.source_kind, fk.source_kind)   AS source_kind,
        COALESCE(bk.scenario,   fk.scenario)       AS scenario,
        COALESCE(bk.account_code, fk.account_code) AS account_code,
        fk.account_name,
        bk.facit_amt,
        fk.fact_amt,
        ROUND((COALESCE(bk.facit_amt, 0) - COALESCE(fk.fact_amt, 0))::numeric, 2) AS diff
    FROM (
        SELECT company_id, period, source_kind, scenario, account_code,
               SUM(amount) OVER (
                   PARTITION BY company_id, LEFT(period, 4), source_kind, scenario, account_code
                   ORDER BY period
                   ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
               ) AS facit_amt
        FROM backup_from_mercur
        WHERE scenario = 'A' AND source_kind IN ('SIE', 'SAFT')
    ) bk
    FULL OUTER JOIN (
        SELECT fb.company_id, fb.period, 'SAFT' AS source_kind, fb.scenario,
               fb.account_code, fb.account_name, fb.amount AS fact_amt
        FROM fact_balances fb
        WHERE fb.scenario = 'A' AND fb.source_kind = 'SAFT'
        UNION ALL
        SELECT fb.company_id, fb.period, 'SIE' AS source_kind, fb.scenario,
               fb.account_code, fb.account_name, fb.amount AS fact_amt
        FROM fact_balances fb
        JOIN sie_pick p
          ON p.company_id = fb.company_id AND p.period = fb.period
         AND p.scenario   = fb.scenario   AND p.picked_kind = fb.source_kind
        WHERE fb.scenario = 'A'
    ) fk
      ON  bk.company_id   = fk.company_id
      AND bk.period       = fk.period
      AND bk.source_kind  = fk.source_kind
      AND bk.scenario     = fk.scenario
      AND bk.account_code = fk.account_code
),
account_diff_monthly AS (
    -- IMP/MAN/IMP_ADJ-grenen: monthly↔monthly rakt av.
    SELECT
        COALESCE(bk.company_id, fk.company_id)     AS company_id,
        COALESCE(bk.period,     fk.period)         AS period,
        COALESCE(bk.source_kind, fk.source_kind)   AS source_kind,
        COALESCE(bk.scenario,   fk.scenario)       AS scenario,
        COALESCE(bk.account_code, fk.account_code) AS account_code,
        fk.account_name,
        bk.amount AS facit_amt,
        fk.amount AS fact_amt,
        ROUND((COALESCE(bk.amount, 0) - COALESCE(fk.amount, 0))::numeric, 2) AS diff
    FROM (
        SELECT company_id, period, source_kind, scenario, account_code, amount
        FROM backup_from_mercur
        WHERE scenario = 'A' AND source_kind IN ('IMP', 'MAN', 'IMP_ADJ')
    ) bk
    FULL OUTER JOIN (
        SELECT company_id, period, source_kind, scenario, account_code, account_name, amount
        FROM fact_balances
        WHERE scenario = 'A' AND source_kind IN ('IMP', 'MAN', 'IMP_ADJ')
    ) fk USING (company_id, period, source_kind, scenario, account_code)
),
account_diff AS (
    SELECT *,
           CASE
               WHEN facit_amt IS NULL THEN 'only_fact'
               WHEN fact_amt  IS NULL THEN 'only_facit'
               WHEN ABS(diff) > GREATEST(1.0, 0.01 * ABS(facit_amt)) THEN 'amount_diff'
               ELSE 'ok'
           END AS status_acc
    FROM (
        SELECT * FROM account_diff_ytd
        UNION ALL
        SELECT * FROM account_diff_monthly
    ) merged
)
SELECT
    account_code,
    account_name,
    facit_amt,
    fact_amt,
    diff,
    status_acc
FROM account_diff
WHERE company_id  = %s
  AND period      = %s
  AND source_kind = %s
ORDER BY
    CASE status_acc
        WHEN 'amount_diff' THEN 0
        WHEN 'only_facit'  THEN 1
        WHEN 'only_fact'   THEN 2
        ELSE 3
    END,
    ABS(diff) DESC NULLS LAST,
    account_code;
```

- [ ] **Step 2: Verifiera att SQL:en parsar och returnerar data**

```powershell
$sql = Get-Content -Raw webapp/backend/sql/coverage_accounts.sql
# psql tar inte %s — kör med psycopg-prefixet via en kort py-snutt
py -c @"
from db import connect
sql = open('webapp/backend/sql/coverage_accounts.sql', encoding='utf-8').read()
with connect(read_only=True) as con:
    rows = con.fetch_dicts(sql, [134, '202604', 'IMP'])
print(f'{len(rows)} rader')
for r in rows[:5]:
    print(r)
"@
```

Förväntat: `>0 rader`, första 5 raderna är `amount_diff`/`only_*` om bolag 134/202604 har mismatch, annars `ok`-rader sorterade på account_code.

- [ ] **Step 3: Sanity-check SE-bolag (SIE-grenen)**

Byt parametrar och kör om mot ett SE-bolag som finns för 202604:

```powershell
py -c @"
from db import connect
sql = open('webapp/backend/sql/coverage_accounts.sql', encoding='utf-8').read()
with connect(read_only=True) as con:
    rows = con.fetch_dicts(sql, [32, '202604', 'SIE'])
print(f'{len(rows)} rader; status-fördelning:')
from collections import Counter
print(Counter(r['status_acc'] for r in rows))
"@
```

Förväntat: status-fördelning där `ok` dominerar (>80% normalt), `amount_diff` bara där facit avviker.

- [ ] **Step 4: Commit**

```bash
git add webapp/backend/sql/coverage_accounts.sql
git commit -m "sql: lägg till coverage_accounts.sql för per-konto-drilldown"
```

---

## Task 3: Lägg till `/api/compare/coverage/accounts`-endpoint

**Files:**
- Modifiera: `webapp/backend/main.py` (efter rad ~345, direkt efter `compare_coverage`)

- [ ] **Step 1: Lägg till SQL-path-konstanten**

I `main.py` runt rad 47, efter `SQL_COVERAGE`:

```python
SQL_COVERAGE_ACCOUNTS = REPO / "webapp" / "backend" / "sql" / "coverage_accounts.sql"
```

- [ ] **Step 2: Lägg till endpoint-funktionen efter `compare_coverage`**

Sätt in efter rad ~345 (sista raden av `compare_coverage`-funktionen, före `# ----- Personnel`-blocket):

```python
_ACCOUNTS_SOURCE_KINDS = {"IMP", "SIE", "SAFT", "MAN", "IMP_ADJ"}


@app.get("/api/compare/coverage/accounts")
async def compare_coverage_accounts(
    company_id:  int = Query(..., ge=1),
    period:      str = Query(..., pattern=r"^\d{6}$"),
    source_kind: str = Query(..., description="IMP|SIE|SAFT|MAN|IMP_ADJ"),
):
    """Per-konto-diff för (bolag, period, källa) — drilldown från täckningsmatrisen.

    För SIE/SAFT YTD-kumuleras backup_from_mercur innan jämförelse mot YTD-fact.
    För IMP/MAN/IMP_ADJ jämförs monthly↔monthly. SIE_PSALDO accepteras inte
    som input — SE-data nås alltid via source_kind='SIE' (mappas internt via
    picked_kind-CTE).
    """
    if source_kind not in _ACCOUNTS_SOURCE_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"source_kind måste vara en av {sorted(_ACCOUNTS_SOURCE_KINDS)}",
        )

    sql = SQL_COVERAGE_ACCOUNTS.read_text(encoding="utf-8")
    with open_db() as con:
        rows = con.fetch_dicts(sql, [company_id, period, source_kind])
        # Hämta company_name separat — query:n returnerar bara konto-nivå.
        name_row = con.execute(
            "SELECT name FROM dim_company WHERE company_id = %s", [company_id]
        ).fetchone()
    company_name = name_row[0] if name_row else None

    out_rows = [
        {
            "account_code": _safe_str(r["account_code"]),
            "account_name": _safe_str(r["account_name"]),
            "facit_amt":    _safe_num(r["facit_amt"]),
            "fact_amt":     _safe_num(r["fact_amt"]),
            "diff":         _safe_num(r["diff"]),
            "status_acc":   _safe_str(r["status_acc"]),
        }
        for r in rows
    ]

    summary = {
        "n_ok":          sum(1 for r in out_rows if r["status_acc"] == "ok"),
        "n_amount_diff": sum(1 for r in out_rows if r["status_acc"] == "amount_diff"),
        "n_only_facit":  sum(1 for r in out_rows if r["status_acc"] == "only_facit"),
        "n_only_fact":   sum(1 for r in out_rows if r["status_acc"] == "only_fact"),
        "facit_sum":     sum(r["facit_amt"] or 0.0 for r in out_rows),
        "fact_sum":      sum(r["fact_amt"]  or 0.0 for r in out_rows),
    }
    # Runda summor till 2 decimaler — matchar SQL ROUND och undviker FP-brus i UI.
    summary["facit_sum"] = round(summary["facit_sum"], 2)
    summary["fact_sum"]  = round(summary["fact_sum"], 2)

    return {
        "company_id":   company_id,
        "company_name": company_name,
        "period":       period,
        "source_kind":  source_kind,
        "rows":         out_rows,
        "summary":      summary,
    }
```

- [ ] **Step 3: Starta backend-dev-servern**

```powershell
py -m uvicorn webapp.backend.main:app --reload --port 8000
```

(I separat terminal — låt servern köra under nästa steg.)

- [ ] **Step 4: Verifiera endpointen med curl**

```powershell
curl.exe -s "http://localhost:8000/api/compare/coverage/accounts?company_id=134&period=202604&source_kind=IMP" | py -m json.tool | Select-Object -First 30
```

Förväntat: JSON med `company_id`, `company_name`, `period`, `source_kind`, `rows` (array), `summary` (objekt med 6 fält). `rows` sorterade som SQL:en specar.

- [ ] **Step 5: Verifiera validerings-felet**

```powershell
curl.exe -s -o $null -w "%{http_code}" "http://localhost:8000/api/compare/coverage/accounts?company_id=134&period=202604&source_kind=SIE_PSALDO"
```

Förväntat: `400`.

```powershell
curl.exe -s -o $null -w "%{http_code}" "http://localhost:8000/api/compare/coverage/accounts?company_id=134&period=2026Q2&source_kind=IMP"
```

Förväntat: `422` (FastAPI pattern-validering).

- [ ] **Step 6: Commit**

```bash
git add webapp/backend/main.py
git commit -m "backend: ny endpoint /api/compare/coverage/accounts för drilldown"
```

---

## Task 4: Frontend-typer + fetch-funktion

**Files:**
- Modifiera: `webapp/frontend/src/api.ts` (lägg till efter `fetchCoverage`, runt rad 142)

- [ ] **Step 1: Lägg till typer + fetch-funktion**

Sätt in direkt efter `fetchCoverage`-funktionen (rad ~142):

```typescript
export interface CoverageAccountRow {
  account_code: string;
  account_name: string | null;
  facit_amt: number | null;
  fact_amt: number | null;
  diff: number | null;
  status_acc: "ok" | "amount_diff" | "only_facit" | "only_fact";
}

export interface CoverageAccountsSummary {
  n_ok: number;
  n_amount_diff: number;
  n_only_facit: number;
  n_only_fact: number;
  facit_sum: number;
  fact_sum: number;
}

export interface CoverageAccountsReport {
  company_id: number;
  company_name: string | null;
  period: string;
  source_kind: string;
  rows: CoverageAccountRow[];
  summary: CoverageAccountsSummary;
}

export async function fetchCoverageAccounts(opts: {
  company_id: number;
  period: string;
  source_kind: string;
}): Promise<CoverageAccountsReport> {
  const p = new URLSearchParams({
    company_id:  String(opts.company_id),
    period:      opts.period,
    source_kind: opts.source_kind,
  });
  return getJSON<CoverageAccountsReport>(`/api/compare/coverage/accounts?${p}`);
}
```

- [ ] **Step 2: Bygg frontend för att fånga typfel**

```powershell
cd webapp/frontend
npm run build
```

Förväntat: build går igenom utan TypeScript-fel. (Inga konsumenter än — bara typkonsistens checkas.)

- [ ] **Step 3: Commit**

```bash
git add webapp/frontend/src/api.ts
git commit -m "frontend/api: lägg till fetchCoverageAccounts + typer"
```

---

## Task 5: Skapa `CoverageAccountsDrawer.tsx`

**Files:**
- Skapa: `webapp/frontend/src/components/CoverageAccountsDrawer.tsx`

Mål: en självstående drawer-komponent som tar `selection`-prop, hämtar data, renderar header + summary + sorterbar tabell. Ingen koppling till `CoverageReport` än — testas via tillfällig hardcoded selection i Task 6.

- [ ] **Step 1: Skriv komponenten**

Full innehåll för `webapp/frontend/src/components/CoverageAccountsDrawer.tsx`:

```typescript
import { useEffect, useMemo, useState } from "react";
import { X, CheckCircle2, AlertTriangle, CircleSlash } from "lucide-react";
import {
  CoverageAccountRow,
  CoverageAccountsReport,
  fetchCoverageAccounts,
} from "../api";
import { fmtCurrency } from "../lib/format";

export interface CoverageAccountsSelection {
  company_id: number;
  company_name: string | null;
  period: string;
  source_kind: string;
}

type SortKey = "account_code" | "account_name" | "facit_amt" | "fact_amt" | "diff" | "status_acc";
type SortDir = "asc" | "desc";

const STATUS_SORT_ORDER: Record<CoverageAccountRow["status_acc"], number> = {
  amount_diff: 0,
  only_facit:  1,
  only_fact:   2,
  ok:          3,
};

const STATUS_LABEL: Record<CoverageAccountRow["status_acc"], string> = {
  amount_diff: "Belopp avviker",
  only_facit:  "Saknas i fact",
  only_fact:   "Extra i fact",
  ok:          "OK",
};

const STATUS_CLS: Record<CoverageAccountRow["status_acc"], string> = {
  amount_diff: "bg-warn/15 text-warn",
  only_facit:  "bg-negative/15 text-negative",
  only_fact:   "bg-warn/15 text-warn",
  ok:          "text-positive",
};

interface Props {
  selection: CoverageAccountsSelection | null;
  onClose: () => void;
}

export function CoverageAccountsDrawer({ selection, onClose }: Props) {
  const [data, setData]       = useState<CoverageAccountsReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError]     = useState<string | null>(null);
  const [hideOk, setHideOk]   = useState(true);
  const [sortKey, setSortKey] = useState<SortKey>("status_acc");
  const [sortDir, setSortDir] = useState<SortDir>("asc");

  // Hämta data när selection ändras
  useEffect(() => {
    if (!selection) { setData(null); return; }
    setLoading(true); setError(null);
    fetchCoverageAccounts({
      company_id:  selection.company_id,
      period:      selection.period,
      source_kind: selection.source_kind,
    })
      .then(setData)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [selection?.company_id, selection?.period, selection?.source_kind]);

  // Escape-stäng + body scroll-lock
  useEffect(() => {
    if (!selection) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [selection, onClose]);

  const sortedRows = useMemo(() => {
    if (!data) return [] as CoverageAccountRow[];
    const rows = hideOk ? data.rows.filter((r) => r.status_acc !== "ok") : data.rows;
    return [...rows].sort((a, b) => {
      let va: string | number, vb: string | number;
      switch (sortKey) {
        case "account_code": va = a.account_code; vb = b.account_code; break;
        case "account_name": va = a.account_name ?? ""; vb = b.account_name ?? ""; break;
        case "facit_amt":    va = a.facit_amt ?? 0; vb = b.facit_amt ?? 0; break;
        case "fact_amt":     va = a.fact_amt  ?? 0; vb = b.fact_amt  ?? 0; break;
        case "diff":         va = Math.abs(a.diff ?? 0); vb = Math.abs(b.diff ?? 0); break;
        case "status_acc":   va = STATUS_SORT_ORDER[a.status_acc]; vb = STATUS_SORT_ORDER[b.status_acc]; break;
      }
      const cmp = va < vb ? -1 : va > vb ? 1 : 0;
      return sortDir === "asc" ? cmp : -cmp;
    });
  }, [data, hideOk, sortKey, sortDir]);

  function toggleSort(k: SortKey) {
    if (sortKey === k) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else { setSortKey(k); setSortDir(k === "diff" || k === "facit_amt" || k === "fact_amt" ? "desc" : "asc"); }
  }

  if (!selection) return null;

  return (
    <>
      {/* Overlay */}
      <div
        className="fixed inset-0 bg-black/40 z-40"
        onClick={onClose}
        aria-hidden
      />
      {/* Drawer */}
      <div
        role="dialog"
        aria-modal="true"
        aria-label={`Konto-diff för ${selection.company_name ?? selection.company_id}, period ${selection.period}, källa ${selection.source_kind}`}
        className="fixed right-0 top-0 bottom-0 w-[640px] max-w-[95vw] bg-bg border-l border-border shadow-xl z-50 flex flex-col"
      >
        {/* Header */}
        <div className="flex items-start justify-between px-4 py-3 border-b border-border">
          <div>
            <h2 className="text-sm font-semibold">
              {selection.company_name ?? `Bolag ${selection.company_id}`}
            </h2>
            <p className="text-xs text-fg-muted mt-0.5">
              Period {selection.period} · Källa <span className="font-mono">{selection.source_kind}</span>
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            autoFocus
            className="text-fg-muted hover:text-fg p-1 -m-1 cursor-pointer"
            aria-label="Stäng"
          >
            <X size={18} />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto">
          {loading && (
            <div className="text-fg-muted text-sm py-8 text-center">Hämtar konto-diff…</div>
          )}
          {error && (
            <div role="alert" className="m-3 bg-negative/10 border border-negative/30 text-negative text-sm rounded-md p-3">
              {error}
            </div>
          )}
          {data && !loading && !error && (
            <>
              {/* Summary chips */}
              <div className="flex flex-wrap items-center gap-4 text-xs px-4 py-3 border-b border-border/50">
                {data.summary.n_amount_diff > 0 && (
                  <span className="flex items-center gap-1.5 text-warn font-semibold">
                    <AlertTriangle size={12} aria-hidden /> {data.summary.n_amount_diff} belopp avviker
                  </span>
                )}
                {data.summary.n_only_facit > 0 && (
                  <span className="flex items-center gap-1.5 text-negative font-semibold">
                    <CircleSlash size={12} aria-hidden /> {data.summary.n_only_facit} saknas i fact
                  </span>
                )}
                {data.summary.n_only_fact > 0 && (
                  <span className="flex items-center gap-1.5 text-warn">
                    <AlertTriangle size={12} aria-hidden /> {data.summary.n_only_fact} extra i fact
                  </span>
                )}
                <span className="flex items-center gap-1.5 text-positive">
                  <CheckCircle2 size={12} aria-hidden /> {data.summary.n_ok} ok
                </span>
                <span className="ml-auto text-fg-muted tabular-nums">
                  Σ facit {fmtCurrency(data.summary.facit_sum)} · fact {fmtCurrency(data.summary.fact_sum)}
                </span>
              </div>

              {/* Filter toggle */}
              <div className="px-4 py-2 border-b border-border/50">
                <label className="text-xs text-fg-muted inline-flex items-center gap-1.5 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={hideOk}
                    onChange={(e) => setHideOk(e.target.checked)}
                  />
                  Visa bara avvikelser
                </label>
              </div>

              {/* Table */}
              {sortedRows.length === 0 ? (
                <div className="text-center text-fg-muted text-sm py-12">
                  {hideOk
                    ? `Inga avvikelser — alla ${data.rows.length} konton stämmer ✓`
                    : "Inga rader"}
                </div>
              ) : (
                <table className="w-full text-xs">
                  <thead className="sticky top-0 bg-surface border-b border-border">
                    <tr>
                      <th
                        onClick={() => toggleSort("account_code")}
                        className="px-3 py-2 text-left font-medium text-fg-muted cursor-pointer hover:bg-elevated"
                      >Konto</th>
                      <th
                        onClick={() => toggleSort("account_name")}
                        className="px-3 py-2 text-left font-medium text-fg-muted cursor-pointer hover:bg-elevated"
                      >Namn</th>
                      <th
                        onClick={() => toggleSort("facit_amt")}
                        className="px-3 py-2 text-right font-medium text-fg-muted cursor-pointer hover:bg-elevated"
                      >Facit</th>
                      <th
                        onClick={() => toggleSort("fact_amt")}
                        className="px-3 py-2 text-right font-medium text-fg-muted cursor-pointer hover:bg-elevated"
                      >Fact</th>
                      <th
                        onClick={() => toggleSort("diff")}
                        className="px-3 py-2 text-right font-medium text-fg-muted cursor-pointer hover:bg-elevated"
                      >Diff</th>
                      <th
                        onClick={() => toggleSort("status_acc")}
                        className="px-3 py-2 text-left font-medium text-fg-muted cursor-pointer hover:bg-elevated"
                      >Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sortedRows.map((r) => (
                      <tr key={r.account_code} className="border-b border-border/50">
                        <td className="px-3 py-1.5 font-mono whitespace-nowrap">{r.account_code}</td>
                        <td className="px-3 py-1.5">{r.account_name ?? "—"}</td>
                        <td className="px-3 py-1.5 text-right tabular-nums">{fmtCurrency(r.facit_amt)}</td>
                        <td className="px-3 py-1.5 text-right tabular-nums">{fmtCurrency(r.fact_amt)}</td>
                        <td className="px-3 py-1.5 text-right tabular-nums">{fmtCurrency(r.diff)}</td>
                        <td className="px-3 py-1.5">
                          <span className={`inline-block px-1.5 py-0.5 rounded text-xs font-medium ${STATUS_CLS[r.status_acc]}`}>
                            {STATUS_LABEL[r.status_acc]}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </>
          )}
        </div>
      </div>
    </>
  );
}
```

- [ ] **Step 2: Bygg frontend för att fånga typfel**

```powershell
cd webapp/frontend
npm run build
```

Förväntat: build går igenom. (Komponenten är inte ännu importerad någonstans — bara typcheck.)

- [ ] **Step 3: Commit**

```bash
git add webapp/frontend/src/components/CoverageAccountsDrawer.tsx
git commit -m "frontend: ny CoverageAccountsDrawer-komponent för drilldown"
```

---

## Task 6: Wire CoverageReport drill-rader till drawer

**Files:**
- Modifiera: `webapp/frontend/src/components/CoverageReport.tsx`

Mål: gör varje rad i drill-tabellen (rad 435–468 i nuvarande fil) klickbar. Klick öppnar drawern med rätt selection. Esc/X stänger.

- [ ] **Step 1: Importera drawer + typ**

I `CoverageReport.tsx` runt rad 6, efter befintliga imports:

```typescript
import { CoverageAccountsDrawer, CoverageAccountsSelection } from "./CoverageAccountsDrawer";
```

- [ ] **Step 2: Lägg till selection-state**

I komponenten runt rad 112 (efter `sortDir`):

```typescript
const [accountsSelection, setAccountsSelection] = useState<CoverageAccountsSelection | null>(null);
```

- [ ] **Step 3: Gör drill-tabellens rader klickbara**

Ersätt nuvarande `<tr>` i `drillRows.map` (rad ~436–467) med klickbar variant. Befintlig kod att ersätta:

```typescript
                {drillRows.map((r, i) => (
                  <tr
                    key={i}
                    className={`border-b border-border/50 transition-colors ${ROW_CLS[r.status]}`}
                  >
```

Ersätt med:

```typescript
                {drillRows.map((r, i) => (
                  <tr
                    key={i}
                    role="button"
                    tabIndex={0}
                    aria-label={`Visa per-konto-diff för ${r.company_name} ${r.period} ${r.source_kind}`}
                    onClick={() => setAccountsSelection({
                      company_id:   r.company_id,
                      company_name: r.company_name,
                      period:       r.period,
                      source_kind:  r.source_kind,
                    })}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        setAccountsSelection({
                          company_id:   r.company_id,
                          company_name: r.company_name,
                          period:       r.period,
                          source_kind:  r.source_kind,
                        });
                      }
                    }}
                    title="Klicka för per-konto-diff"
                    className={`border-b border-border/50 transition-colors cursor-pointer ${ROW_CLS[r.status]}`}
                  >
```

- [ ] **Step 4: Rendera drawer-komponenten**

Hitta sista `</div>`-blocken i `return`-uttrycket (runt rad 477). Direkt före komponentens yttersta `</div>` (rad 477), lägg till:

```typescript
      <CoverageAccountsDrawer
        selection={accountsSelection}
        onClose={() => setAccountsSelection(null)}
      />
```

Strukturen ska bli:

```typescript
      {/* Drill-down --------------------------------------------------- */}
      {drill && (
        <div className="space-y-3">
          ...
        </div>
      )}

      <CoverageAccountsDrawer
        selection={accountsSelection}
        onClose={() => setAccountsSelection(null)}
      />
    </div>
  );
}
```

- [ ] **Step 5: Starta frontend + backend, testa i webbläsare**

I två separata terminaler:

```powershell
# Terminal 1: backend
py -m uvicorn webapp.backend.main:app --reload --port 8000

# Terminal 2: frontend
cd webapp/frontend
npm run dev
```

Öppna `http://localhost:5173`, navigera till täckningssidan. Klicka en cell i matrisen → drill-tabellen syns. Klicka en rad i drill-tabellen → drawer öppnas till höger.

Kontrollera:
- Header visar bolagsnamn, period, källa.
- Summary-chips visar n_amount_diff/n_only_facit/n_only_fact/n_ok + Σ-summor.
- Tabell visar bara avvikelser by default (checkbox "Visa bara avvikelser" ikryssad).
- Avbocka checkboxen → ok-rader syns.
- Klicka kolumnrubrik → sorteringen ändras.
- Tryck Escape → drawer stängs.
- Klicka × → drawer stängs.
- Klicka utanför drawer (overlay) → drawer stängs.

- [ ] **Step 6: Verifiera body scroll-lock**

Med drawer öppen, försök scrolla bakgrunden — ska vara låst. Stäng drawer → scroll fungerar igen.

- [ ] **Step 7: Commit**

```bash
git add webapp/frontend/src/components/CoverageReport.tsx
git commit -m "frontend: klick på drill-rad öppnar per-konto-drawer"
```

---

## Task 7: Uppdatera `compare_coverage.sql` med per-konto-mismatch

**Files:**
- Modifiera: `webapp/backend/sql/compare_coverage.sql`

Mål: byt mismatch-CASE från sum-baserad (bara IMP/MAN/IMP_ADJ) till `EXISTS`-test mot per-konto-diff (alla källor inklusive SIE/SAFT). Matris-svaret blir striktare.

- [ ] **Step 1: Snapshot före — räkna mismatch-celler per källa**

Spara baseline:

```powershell
py -c @"
from db import connect
sql = open('webapp/backend/sql/compare_coverage.sql', encoding='utf-8').read()
wrapped = f'SELECT source_kind, status, COUNT(*) AS n FROM (\\n{sql}\\n) c WHERE period >= %s GROUP BY 1,2 ORDER BY 1,2'
with connect(read_only=True) as con:
    rows = con.fetch_dicts(wrapped, ['202601'])
for r in rows: print(r)
"@ | Out-File -Encoding utf8 _scratch/coverage_status_before.txt
```

- [ ] **Step 2: Mät query-tid före**

```powershell
$sql = Get-Content -Raw webapp/backend/sql/compare_coverage.sql
psql "$env:DATABASE_URL" -c "EXPLAIN ANALYZE SELECT * FROM (`n$sql`n) c WHERE period >= '202601'" 2>&1 | Select-String "Execution Time"
```

Förväntat: notera siffran (typiskt 50–200 ms).

- [ ] **Step 3: Uppdatera SQL-filen**

Öppna `webapp/backend/sql/compare_coverage.sql`. Ändringar:

**a) Ersätt kommentarsblocket överst (rad 1–23) med:**

```sql
-- Jämförelse Mercur-facit (`backup_from_mercur`) vs `fact_balances` per
-- (bolag, period, källa, scenario).
--
-- Status:
--   'missing'      — facit har rader, fact_balances har inga (riktigt saknad data)
--   'missing_zero' — facit har rader men summan ≈ 0 för SIE/SAFT (Mercur har
--                    pre-allokerat tomma noll-rader för bolag utan månadsbevegelse;
--                    ingen riktig data saknas, bara harmlös pre-allokering)
--   'extra'        — fact_balances har rader, facit har inga (utanför facit-scope)
--   'mismatch'     — minst ett konto avviker (per-konto-test via account_diff-CTE,
--                    se nedan). Gäller alla källor. För SIE/SAFT YTD-kumuleras
--                    backup till samma format som fact innan jämförelse.
--   'ok'           — båda har rader, alla konton stämmer
--
-- account_diff-CTE:n delar logik med coverage_accounts.sql (drilldown-endpoint).
-- Vid divergens — håll båda i sync. Tröskel per konto: |diff| > 1 OCH > 1%×|facit|.
--
-- Normalisering: fact_balances har SE-data som både SIE (transaktioner) och
-- SIE_PSALDO (periodsaldon från samma fil). Vi väljer SIE om den finns,
-- annars SIE_PSALDO. På så vis matchar backup.SIE direkt mot fact-SIE.
-- Begränsa till utfall (scenario='A') — budget (B) ligger utanför facit.
```

**b) Lägg till `account_diff`-CTE direkt efter `fact_agg`-CTE:n (efter rad 66, före `SELECT`-blocket).**

Insättning efter raden `fact_agg AS (... UNION ALL SELECT * FROM fact_other)`:

```sql
,
-- Per-konto-diff (delad logik med coverage_accounts.sql). UNION ALL av
-- YTD-grenen (SIE/SAFT) och monthly-grenen (IMP/MAN/IMP_ADJ). EXISTS-testet
-- nedan använder detta för att flagga mismatch på matris-nivå.
account_diff AS (
    SELECT * FROM (
        -- YTD-gren: SIE/SAFT
        SELECT
            COALESCE(bk.company_id, fk.company_id)     AS company_id,
            COALESCE(bk.period,     fk.period)         AS period,
            COALESCE(bk.source_kind, fk.source_kind)   AS source_kind,
            COALESCE(bk.scenario,   fk.scenario)       AS scenario,
            COALESCE(bk.account_code, fk.account_code) AS account_code,
            bk.facit_amt,
            fk.fact_amt,
            ROUND((COALESCE(bk.facit_amt, 0) - COALESCE(fk.fact_amt, 0))::numeric, 2) AS diff
        FROM (
            SELECT company_id, period, source_kind, scenario, account_code,
                   SUM(amount) OVER (
                       PARTITION BY company_id, LEFT(period, 4), source_kind, scenario, account_code
                       ORDER BY period
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                   ) AS facit_amt
            FROM backup_from_mercur
            WHERE scenario = 'A' AND source_kind IN ('SIE', 'SAFT')
        ) bk
        FULL OUTER JOIN (
            SELECT fb.company_id, fb.period, 'SAFT' AS source_kind, fb.scenario,
                   fb.account_code, fb.amount AS fact_amt
            FROM fact_balances fb
            WHERE fb.scenario = 'A' AND fb.source_kind = 'SAFT'
            UNION ALL
            SELECT fb.company_id, fb.period, 'SIE' AS source_kind, fb.scenario,
                   fb.account_code, fb.amount AS fact_amt
            FROM fact_balances fb
            JOIN sie_pick p
              ON p.company_id = fb.company_id AND p.period = fb.period
             AND p.scenario   = fb.scenario   AND p.picked_kind = fb.source_kind
            WHERE fb.scenario = 'A'
        ) fk
          ON  bk.company_id   = fk.company_id
          AND bk.period       = fk.period
          AND bk.source_kind  = fk.source_kind
          AND bk.scenario     = fk.scenario
          AND bk.account_code = fk.account_code

        UNION ALL

        -- Monthly-gren: IMP/MAN/IMP_ADJ
        SELECT
            COALESCE(bk.company_id, fk.company_id)     AS company_id,
            COALESCE(bk.period,     fk.period)         AS period,
            COALESCE(bk.source_kind, fk.source_kind)   AS source_kind,
            COALESCE(bk.scenario,   fk.scenario)       AS scenario,
            COALESCE(bk.account_code, fk.account_code) AS account_code,
            bk.amount AS facit_amt,
            fk.amount AS fact_amt,
            ROUND((COALESCE(bk.amount, 0) - COALESCE(fk.amount, 0))::numeric, 2) AS diff
        FROM (
            SELECT company_id, period, source_kind, scenario, account_code, amount
            FROM backup_from_mercur
            WHERE scenario = 'A' AND source_kind IN ('IMP', 'MAN', 'IMP_ADJ')
        ) bk
        FULL OUTER JOIN (
            SELECT company_id, period, source_kind, scenario, account_code, amount
            FROM fact_balances
            WHERE scenario = 'A' AND source_kind IN ('IMP', 'MAN', 'IMP_ADJ')
        ) fk USING (company_id, period, source_kind, scenario, account_code)
    ) merged
    WHERE
        -- Filtrera så bara avvikelser exponeras till EXISTS-testet — sparar arbete.
        (facit_amt IS NULL)
        OR (fact_amt IS NULL)
        OR (ABS(ROUND((COALESCE(facit_amt,0) - COALESCE(fact_amt,0))::numeric, 2))
              > GREATEST(1.0, 0.01 * ABS(COALESCE(facit_amt, 0))))
)
```

**c) Ersätt `'mismatch'`-grenen i CASE-uttrycket (rad ~88–94).**

Befintlig kod att ersätta:

```sql
        -- Belopp-mismatch flaggas bara när monthly↔monthly är jämförbart
        -- (IMP/MAN/IMP_ADJ). SIE/SAFT skiljer per definition (YTD vs M).
        WHEN COALESCE(b.source_kind, f.source_kind) IN ('IMP', 'MAN', 'IMP_ADJ')
             AND ABS(COALESCE(b.total, 0) - COALESCE(f.total, 0)) > 1
             AND ABS(COALESCE(b.total, 0) - COALESCE(f.total, 0))
                 > 0.01 * NULLIF(ABS(COALESCE(b.total, 0)), 0)
        THEN 'mismatch'
```

Ersätt med:

```sql
        -- Per-konto-mismatch (gäller alla källor). account_diff är förfiltrerat
        -- så EXISTS bara returnerar något om minst ett konto faktiskt avviker.
        WHEN EXISTS (
            SELECT 1 FROM account_diff ad
            WHERE ad.company_id  = COALESCE(b.company_id, f.company_id)
              AND ad.period      = COALESCE(b.period, f.period)
              AND ad.source_kind = COALESCE(b.source_kind, f.source_kind)
              AND ad.scenario    = COALESCE(b.scenario, f.scenario)
        ) THEN 'mismatch'
```

- [ ] **Step 4: Verifiera att SQL:en parsar**

```powershell
py -c @"
from db import connect
sql = open('webapp/backend/sql/compare_coverage.sql', encoding='utf-8').read()
wrapped = f'SELECT COUNT(*) AS n FROM (\\n{sql}\\n) c WHERE period >= %s'
with connect(read_only=True) as con:
    print(con.execute(wrapped, ['202601']).fetchone())
"@
```

Förväntat: ett heltal, samma storleksordning som före (radantal förändras inte; bara status-fördelning).

- [ ] **Step 5: Snapshot efter**

```powershell
py -c @"
from db import connect
sql = open('webapp/backend/sql/compare_coverage.sql', encoding='utf-8').read()
wrapped = f'SELECT source_kind, status, COUNT(*) AS n FROM (\\n{sql}\\n) c WHERE period >= %s GROUP BY 1,2 ORDER BY 1,2'
with connect(read_only=True) as con:
    rows = con.fetch_dicts(wrapped, ['202601'])
for r in rows: print(r)
"@ | Out-File -Encoding utf8 _scratch/coverage_status_after.txt
```

Jämför:

```powershell
git diff --no-index _scratch/coverage_status_before.txt _scratch/coverage_status_after.txt
```

Förväntat:
- **SIE-mismatch** ökar från 0 till >0 (där facit faktiskt avviker).
- **SAFT-mismatch** ökar från 0 till >0.
- **IMP-mismatch** kan öka markant (per-konto fångar compensating errors).
- Totala radantalet per (source_kind) ändras inte — bara status-fördelning flyttar mellan `ok` och `mismatch`.

- [ ] **Step 6: Mät query-tid efter**

```powershell
$sql = Get-Content -Raw webapp/backend/sql/compare_coverage.sql
psql "$env:DATABASE_URL" -c "EXPLAIN ANALYZE SELECT * FROM (`n$sql`n) c WHERE period >= '202601'" 2>&1 | Select-String "Execution Time"
```

Förväntat: < 1000 ms. Om > 2000 ms — överväg att lägga ett `b-tree`-index på `backup_from_mercur(company_id, period, source_kind, scenario, account_code)` och re-mät.

- [ ] **Step 7: Verifiera manuellt i webbläsaren**

Med backend + frontend igång (Task 6):
- Reload täckningssidan.
- Bekräfta att SIE/SAFT-celler nu kan visa gul (mismatch) — inte bara grön/röd.
- Klicka en SIE-mismatch-cell → klicka en drill-rad → drawern visar konton med `amount_diff`/`only_*`.
- Klicka en bolagsrad som var ok före och fortfarande är ok → drawer visar tomt diff-läge ("Inga avvikelser — alla N konton stämmer ✓").

- [ ] **Step 8: Commit**

```bash
git add webapp/backend/sql/compare_coverage.sql
git commit -m "sql: per-konto-mismatch i compare_coverage (gäller även SIE/SAFT)"
```

---

## Verifikation efter alla tasks

- [ ] `git log --oneline -7` visar de 6 commits från Task 2–7 (Task 1 är prototype, ingen commit).
- [ ] `_scratch/` är gitignored och inte tracked.
- [ ] Backend `py -m uvicorn webapp.backend.main:app --port 8000` startar utan fel.
- [ ] `curl http://localhost:8000/api/compare/coverage/accounts?company_id=134&period=202604&source_kind=IMP` returnerar `200` med korrekt JSON-form.
- [ ] Frontend `npm run build` går igenom utan TS-fel.
- [ ] Täckningssidan i webbläsare:
  - matrisens SIE/SAFT-celler kan vara gula (mismatch),
  - klick på drill-rad öppnar drawer,
  - drawer visar per-konto-diff korrekt,
  - Escape/X/overlay-klick stänger drawer.

## Open follow-ups (out of scope)

- YTD-jämförelse även för **IMP** (FI/DK/DE).
- Export av diff-listan från drawer.
- Histogram över |diff| på matrisen.
- Refaktorera `account_diff`-CTE till delad SQL-fil (idag duplicerad i `compare_coverage.sql` och `coverage_accounts.sql`).
