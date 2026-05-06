"""FastAPI-backend för Finance Reporting GUI.

Kör (dev):
    py -m uvicorn webapp.backend.main:app --reload --port 8000

Endpoints:
    GET /api/companies                         — lista bolag (med country, currency)
    GET /api/periods                           — perioder med data
    GET /api/report/options?company_id=X&period=Y — tillgängliga source_kinds
    GET /api/report/pnl?company_id=X&period=Y[&source_kind=]  — P&L-rapport (tree + KPIs + budget YTD)
    GET /api/compare/coverage                  — backup vs fact_balances täckning
    GET /api/personnel/countries               — länder med personaldata
    GET /api/personnel/summary?country=X       — pivot per bolag × år (UB/Began/Slutat)
    GET /api/personnel/employees?company_id=X  — drilldown till individnivå
    GET /api/report/pivot?country=X&period_from=Y&period_to=Z&granularity=quarter[&...]
                                              — flerperiods/flerbolags pivot-rapport
    GET  /api/counterparties/periods          — perioder med rapport / SAF-T-filer
    GET  /api/counterparties?period=Y         — full motpartsdata (CSV + drilldown)
    POST /api/counterparties/run              — trigga check_counterparties.py
    GET  /api/counterparties/run/status       — pågående / senaste körning
"""
from __future__ import annotations

import math
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import duckdb
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from webapp.backend.kpi import compute_kpis  # noqa: E402
from webapp.backend.layout import reorder_rows  # noqa: E402
from webapp.backend.period_utils import (  # noqa: E402
    prev_period, year_start, period_buckets, ltm_bucket, ytd_bucket, Bucket,
)
from webapp.backend import counterparty_data, counterparty_runner  # noqa: E402

DB_PATH = REPO / "data" / "finance.duckdb"
SQL_PATH = REPO / "webapp" / "backend" / "sql" / "report_pnl.sql"
SQL_COVERAGE = REPO / "webapp" / "backend" / "sql" / "compare_coverage.sql"
SQL_PERSONNEL = REPO / "webapp" / "backend" / "sql" / "personnel_summary.sql"
SQL_PIVOT = REPO / "webapp" / "backend" / "sql" / "report_pivot.sql"
SQL_SUP_BY_SUPPLIER = REPO / "webapp" / "backend" / "sql" / "suppliers_by_supplier.sql"
SQL_SUP_BY_CATEGORY = REPO / "webapp" / "backend" / "sql" / "suppliers_by_category.sql"

# ----- Connection lifecycle ---------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not DB_PATH.exists():
        raise RuntimeError(f"DuckDB-warehouset saknas: {DB_PATH}")
    yield


def db() -> duckdb.DuckDBPyConnection:
    """Öppnar en ny read-only connection per anrop — undviker delat tillstånd."""
    return duckdb.connect(str(DB_PATH), read_only=True)


# ----- App --------------------------------------------------------------------

app = FastAPI(title="Finance Reporting API", lifespan=lifespan)

# CORS för Vite-dev-server (port 5173)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ----- Helpers ----------------------------------------------------------------

def _safe_num(v):
    """JSON kan inte serialisera NaN. Konvertera till None."""
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def _safe_str(v):
    """Pandas konverterar NULL strängar till NaN. Konvertera till None."""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    return str(v)


def _safe_date(v):
    """DuckDB DATE → ISO-datum (YYYY-MM-DD) eller None. Hanterar NaT, NaN, None."""
    if v is None:
        return None
    try:
        import pandas as _pd
        if _pd.isna(v):
            return None
    except (ImportError, TypeError, ValueError):
        pass
    if hasattr(v, "date") and callable(v.date):
        return v.date().isoformat()
    try:
        return v.isoformat()
    except AttributeError:
        return None


# ----- Endpoints --------------------------------------------------------------

@app.get("/api/health")
async def health():
    return {"status": "ok", "db": str(DB_PATH)}


@app.get("/api/companies")
async def list_companies():
    """Bolag som har P&L-data i någon period (filtrerade på consolidated)."""
    with db() as con:
        rows = con.execute(
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
        ).df().to_dict("records")
    return {"companies": rows}


@app.get("/api/periods")
async def list_periods(company_id: int | None = Query(None)):
    """Alla perioder med data, eller filtrerat per bolag."""
    with db() as con:
        if company_id is None:
            rows = con.execute(
                """SELECT period, COUNT(DISTINCT company_id) AS n_companies
                   FROM fact_balances GROUP BY period ORDER BY period DESC"""
            ).df().to_dict("records")
        else:
            rows = con.execute(
                """SELECT period FROM fact_balances WHERE company_id = ?
                   GROUP BY period ORDER BY period DESC""",
                [company_id],
            ).df().to_dict("records")
    return {"periods": rows}


@app.get("/api/report/options")
async def report_options(
    company_id: int = Query(..., description="dim_company.company_id"),
    period: str = Query(..., description="YYYYMM"),
):
    """Returnerar tillgängliga utfalls-source_kinds för (bolag, period).

    MAN exkluderas — den är reserverad för budget-kolumnen (scenario B).
    """
    with db() as con:
        rows = con.execute(
            """SELECT source_kind, COUNT(*) AS n_rows
               FROM fact_balances
               WHERE company_id = ? AND period = ? AND source_kind != 'MAN'
               GROUP BY source_kind
               ORDER BY source_kind""",
            [company_id, period],
        ).fetchall()
    return {"sources": [{"value": r[0], "n_rows": int(r[1])} for r in rows]}


@app.get("/api/report/pnl")
async def pnl_report(
    company_id: int = Query(..., description="dim_company.company_id"),
    period: str = Query(..., description="YYYYMM"),
    source_kind: str | None = Query(None, description="Source override (NULL/saknas = auto)"),
):
    """P&L-rapport: tree (raw, SIE-konvention) + kpis (post-flip).

    Tabellen: scenario='A', källa via best_source eller user override (aldrig MAN).
    Budget YTD-kolumn: scenario='B', källa='MAN' (hårdkodat).
    """
    try:
        prev = prev_period(period)
        ystart = year_start(period)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    src = source_kind or None  # tom sträng → None (auto)
    sql = SQL_PATH.read_text(encoding="utf-8")

    def _params(src_override: str | None, scenario: str | None):
        return [src_override, company_id, ystart, period,    # best_source (4)
                company_id, ystart, period, scenario,        # raw_balances (4)
                prev, period]                                # balances (2)

    with db() as con:
        info = con.execute(
            "SELECT company_id, name, country, currency FROM dim_company WHERE company_id = ?",
            [company_id],
        ).fetchone()
        if info is None:
            raise HTTPException(status_code=404, detail=f"Bolag {company_id} hittades inte")
        df_a = con.execute(sql, _params(src, "A")).df()
        df_b = con.execute(sql, _params("MAN", "B")).df()

    # Bygg lookup: account_id → budget YTD (scenario B)
    budget_ytd_by_id: dict[str, float | None] = {}
    for r in df_b.to_dict("records"):
        budget_ytd_by_id[str(r["account_id"])] = _safe_num(r.get("amount_ytd"))

    rows = []
    for r in df_a.to_dict("records"):
        aid = _safe_str(r["account_id"])
        rows.append({
            "account_id":        aid,
            "parent_id":         _safe_str(r.get("parent_id")),
            "label_sv":          _safe_str(r["label_sv"]),
            "label_en":          _safe_str(r["label_en"]),
            "is_aggregated":     bool(r["is_aggregated"]),
            "depth":             int(r["depth"]),
            "account_code":      _safe_str(r.get("account_code")),
            "leaf_label":        _safe_str(r.get("leaf_label")),
            "amount_month":      _safe_num(r.get("amount_month")),
            "amount_ytd":        _safe_num(r.get("amount_ytd")),
            "amount_ytd_budget": budget_ytd_by_id.get(aid),
            "sort_path":         _safe_str(r["sort_path"]),
        })

    # KPI-beräkning innan sort_path skrivs om (KPI:erna är ändå anchor-baserade)
    kpis_dict = compute_kpis(rows)

    # Budget-KPI:er — kör samma compute_kpis på scenario B-rader.
    # Vi behöver bara YTD; bygger minimala rader med amount_ytd = budget-YTD.
    rows_b = [
        {
            "is_aggregated": bool(r["is_aggregated"]),
            "account_id":    str(r["account_id"]),
            "amount_month":  None,
            "amount_ytd":    _safe_num(r.get("amount_ytd")),
        }
        for r in df_b.to_dict("records")
    ]
    kpis_b_dict = compute_kpis(rows_b)

    # Sortera om enligt Mercur-ordningen via webapp/config/pnl_layout.yaml
    rows = reorder_rows(rows)
    kpis = []
    for kid, k in kpis_dict.items():
        kb = kpis_b_dict.get(kid, {})
        kpis.append({
            "id":                kid,
            "label_sv":          k["label_sv"],
            "label_en":          k["label_en"],
            "anchor":            k["anchor"],
            "format":            k["format"],
            "emphasis":          k["emphasis"],
            "amount_month":      _safe_num(k["amount_month"]),
            "amount_ytd":        _safe_num(k["amount_ytd"]),
            "amount_ytd_budget": _safe_num(kb.get("amount_ytd")),
        })

    return {
        "company": {
            "company_id": info[0], "name": info[1],
            "country":    info[2], "currency": info[3],
        },
        "period":      period,
        "prev_period": prev,
        "year_start":  ystart,
        "rows":        rows,
        "kpis":        kpis,
    }


@app.get("/api/compare/coverage")
async def compare_coverage():
    """Jämförelse backup_from_mercur vs fact_balances per (bolag, period, källa, scenario)."""
    sql = SQL_COVERAGE.read_text(encoding="utf-8")
    with db() as con:
        rows = con.execute(sql).df().to_dict("records")
    return [
        {
            "company_id":   int(r["company_id"]) if r["company_id"] is not None else None,
            "company_name": _safe_str(r["company_name"]),
            "country":      _safe_str(r["country"]),
            "period":       _safe_str(r["period"]),
            "source_kind":  _safe_str(r["source_kind"]),
            "scenario":     _safe_str(r["scenario"]),
            "backup_rows":  _safe_num(r["backup_rows"]),
            "fact_rows":    _safe_num(r["fact_rows"]),
            "backup_sum":   _safe_num(r["backup_sum"]),
            "fact_sum":     _safe_num(r["fact_sum"]),
            "status":       _safe_str(r["status"]),
        }
        for r in rows
    ]


# ----- Personnel ---------------------------------------------------------------

@app.get("/api/personnel/countries")
async def personnel_countries():
    """Länder med data i fact_personnel + radantal + senaste snapshot."""
    with db() as con:
        rows = con.execute(
            """SELECT country,
                      COUNT(*)                         AS n_rows,
                      COUNT(DISTINCT company_id)       AS n_companies,
                      MAX(snapshot_date)               AS snapshot_date
               FROM fact_personnel
               GROUP BY country
               ORDER BY country"""
        ).fetchall()
    return {
        "countries": [
            {
                "country":       r[0],
                "n_rows":        int(r[1]),
                "n_companies":   int(r[2]),
                "snapshot_date": r[3].isoformat() if r[3] is not None else None,
            }
            for r in rows
        ]
    }


@app.get("/api/personnel/summary")
async def personnel_summary(country: str = Query(..., description="Sweden|Norway|Finland")):
    """Pivot per bolag × år med UB/Began/Slutat.

    År-spannet härleds dynamiskt: från MIN(YEAR(employed_from))-clamp 4 år bakåt
    från innevarande år, t.o.m. innevarande år + 1 (för att fånga 'Slutat' som
    ligger i framtiden, t.ex. uppsägning till nästa år).
    """
    with db() as con:
        bounds = con.execute(
            """SELECT MIN(EXTRACT(year FROM employed_from))::INTEGER,
                      MAX(EXTRACT(year FROM employed_from))::INTEGER
               FROM fact_personnel WHERE country = ?""",
            [country],
        ).fetchone()
    if bounds is None or bounds[0] is None:
        return {"country": country, "years": [], "rows": []}

    from datetime import date as _date
    today_year = _date.today().year
    end_year = today_year + 1
    start_year = max(bounds[0], today_year - 3)
    if start_year > end_year:
        start_year = end_year
    years = list(range(start_year, end_year + 1))

    sql = SQL_PERSONNEL.read_text(encoding="utf-8")
    with db() as con:
        df = con.execute(sql, [years, country]).df()

    # Pivotera till en rad per bolag med dict {year: {ub, began, slutat}}
    by_company: dict[int, dict] = {}
    for r in df.to_dict("records"):
        cid = int(r["company_id"])
        rec = by_company.setdefault(cid, {
            "company_id":   cid,
            "company_name": _safe_str(r["company_name"]),
            "years":        {},
        })
        rec["years"][str(int(r["year"]))] = {
            "ub":     int(r["ub"]),
            "began":  int(r["began"]),
            "slutat": int(r["slutat"]),
        }

    rows = sorted(by_company.values(), key=lambda x: (x["company_name"] or "").lower())
    return {"country": country, "years": years, "rows": rows}


@app.get("/api/personnel/employees")
async def personnel_employees(
    company_id: int = Query(..., description="dim_company.company_id"),
):
    """Anställda för ett bolag, sorterade på employed_from desc (NULL sist)."""
    with db() as con:
        meta = con.execute(
            "SELECT name, country, currency FROM dim_company WHERE company_id = ?",
            [company_id],
        ).fetchone()
        if meta is None:
            raise HTTPException(status_code=404, detail=f"Bolag {company_id} hittades inte")
        df = con.execute(
            """SELECT employee_name, title, birth_date,
                      employed_from, employed_to, termination_reason,
                      employment_pct, productivity, billable_pct,
                      gender, category, salary_local,
                      location, apprenticeship_end, pension_apprentice
               FROM fact_personnel
               WHERE company_id = ?
               ORDER BY employed_from DESC NULLS LAST, employee_name""",
            [company_id],
        ).df()

    employees = []
    for r in df.to_dict("records"):
        employees.append({
            "employee_name":      _safe_str(r["employee_name"]),
            "title":              _safe_str(r["title"]),
            "birth_date":         _safe_date(r["birth_date"]),
            "employed_from":      _safe_date(r["employed_from"]),
            "employed_to":        _safe_date(r["employed_to"]),
            "termination_reason": _safe_str(r["termination_reason"]),
            "employment_pct":     _safe_num(r["employment_pct"]),
            "productivity":       _safe_num(r["productivity"]),
            "billable_pct":       _safe_num(r["billable_pct"]),
            "gender":             _safe_str(r["gender"]),
            "category":           _safe_str(r["category"]),
            "salary_local":       _safe_num(r["salary_local"]),
            "location":           _safe_str(r["location"]),
            "apprenticeship_end": _safe_date(r["apprenticeship_end"]),
            "pension_apprentice": _safe_str(r["pension_apprentice"]),
        })
    return {
        "company": {
            "company_id": company_id,
            "name":       meta[0],
            "country":    meta[1],
            "currency":   meta[2],
        },
        "employees": employees,
    }


# ----- Pivot report ------------------------------------------------------------

_GRANULARITIES = {"month", "quarter", "half", "year"}
_REPORT_CURRENCIES = {"SEK", "LOCAL"}


def _build_buckets(
    period_from: str, period_to: str, granularity: str,
    include_ltm: bool, include_ytd: bool,
) -> list[Bucket]:
    buckets = period_buckets(period_from, period_to, granularity)
    if include_ytd:
        buckets = buckets + [ytd_bucket(period_to)]
    if include_ltm:
        buckets = buckets + [ltm_bucket(period_to)]
    return buckets


def _resolve_company_ids(
    con: duckdb.DuckDBPyConnection,
    country: str | None,
    company_ids_csv: str | None,
) -> list[int]:
    """Returnera listan av bolag att rapportera. Konsoliderade rader exkluderas
    eftersom de inte har egen utfallsdata."""
    if company_ids_csv:
        try:
            ids = [int(x.strip()) for x in company_ids_csv.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(status_code=400, detail="Ogiltigt company_ids-format")
        if not ids:
            raise HTTPException(status_code=400, detail="company_ids saknas")
        return ids
    if country:
        rows = con.execute(
            """SELECT company_id FROM dim_company
               WHERE country = ? AND COALESCE(kind, '') != 'consolidated'
               ORDER BY company_id""",
            [country],
        ).fetchall()
        return [r[0] for r in rows]
    raise HTTPException(status_code=400, detail="Ange country eller company_ids")


@app.get("/api/report/pivot")
async def report_pivot(
    country: str | None = Query(None, description="Hela landet (alt. company_ids)"),
    company_ids: str | None = Query(None, description="Kommaseparerad lista med company_id"),
    period_from: str = Query(..., description="YYYYMM, inklusive"),
    period_to: str = Query(..., description="YYYYMM, inklusive"),
    granularity: str = Query("quarter", description="month|quarter|half|year"),
    report_currency: str = Query("LOCAL", description="SEK eller LOCAL"),
    include_ltm: bool = Query(False, description="Lägg till LTM-kolumn"),
    include_ytd: bool = Query(False, description="Lägg till YTD-kolumn (jan→period_to)"),
    scenario: str = Query("A", description="A=utfall, B=budget"),
    source_kind: str | None = Query(None, description="Tvinga viss källa (annars auto per land)"),
):
    """Pivot: bolag × period-buckets × kontoträd. Ger alla bolag i ett land eller
    explicit lista. Tids-granularitet (År/Halvår/Kvartal/Månad) plus valfri LTM-kolumn.
    Belopp returneras i bolagets lokala valuta (LOCAL) eller konverterat till SEK."""
    if granularity not in _GRANULARITIES:
        raise HTTPException(status_code=400, detail=f"granularity måste vara en av {_GRANULARITIES}")
    if report_currency not in _REPORT_CURRENCIES:
        # Tysta degradering: andra valutor → LOCAL (planerat stöd i steg-2).
        report_currency = "LOCAL"
    if scenario not in ("A", "B"):
        raise HTTPException(status_code=400, detail="scenario måste vara 'A' eller 'B'")

    try:
        buckets = _build_buckets(period_from, period_to, granularity,
                                  include_ltm, include_ytd)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not buckets:
        return {"buckets": [], "companies": [], "rows": [], "report_currency": report_currency}

    with db() as con:
        company_ids_list = _resolve_company_ids(con, country, company_ids)
        if not company_ids_list:
            return {"buckets": [], "companies": [], "rows": [], "report_currency": report_currency}

        # Bolags-metadata för rad-rendering
        comp_rows = con.execute(
            """SELECT company_id, name, country, currency, kind, parent_id, acquisition_year
               FROM dim_company
               WHERE company_id IN (SELECT UNNEST(?::INTEGER[]))
               ORDER BY country, name""",
            [company_ids_list],
        ).fetchall()
        companies = [
            {
                "company_id":       int(r[0]),
                "name":             _safe_str(r[1]),
                "country":          _safe_str(r[2]),
                "currency":         _safe_str(r[3]),
                "kind":             _safe_str(r[4]),
                "parent_id":        int(r[5]) if r[5] is not None else None,
                "acquisition_year": int(r[6]) if r[6] is not None else None,
            }
            for r in comp_rows
        ]

        # Bygg VALUES-stränpermitt + parametrar
        bucket_values_clause = "VALUES " + ", ".join(["(?, ?, ?)"] * len(buckets))
        bucket_params = [v for b in buckets for v in (b.key, b.start, b.end)]
        sql_template = SQL_PIVOT.read_text(encoding="utf-8")
        sql = sql_template.replace("{bucket_values}", bucket_values_clause)

        params = (
            bucket_params
            + [company_ids_list, source_kind, scenario, report_currency]
        )
        df = con.execute(sql, params).df()

    # Pivota till struktur: en rad per (account_id, parent_id, sort_path) med by_company-dict
    rows_by_account: dict[str, dict] = {}
    for r in df.to_dict("records"):
        aid = _safe_str(r["account_id"])
        if aid is None:
            continue
        rec = rows_by_account.get(aid)
        if rec is None:
            rec = {
                "account_id":    aid,
                "parent_id":     _safe_str(r.get("parent_id")),
                "label_sv":      _safe_str(r.get("label_sv")),
                "label_en":      _safe_str(r.get("label_en")),
                "is_aggregated": bool(r.get("is_aggregated")),
                "depth":         int(r.get("depth") or 0),
                "account_code":  _safe_str(r.get("account_code")),
                "leaf_label":    _safe_str(r.get("leaf_label")),
                "sort_path":     _safe_str(r.get("sort_path")),
                "by_company":    {},
            }
            rows_by_account[aid] = rec
        cid = str(int(r["company_id"]))
        bk = _safe_str(r["bucket_key"])
        if bk is None:
            continue
        cell_map = rec["by_company"].setdefault(cid, {})
        cell_map[bk] = _safe_num(r["amount"])

    rows = sorted(rows_by_account.values(),
                  key=lambda x: (x["sort_path"] or "", x["account_id"]))

    # Sortera om enligt Mercur-ordning (Total Sales först → Total Direct Cost → OpEx → ...)
    # reorder_rows() prefixar sort_path med 000|, 001|, ... så frontend kan sortera direkt på sort_path.
    rows = reorder_rows(rows)

    # KPI-beräkning per (bolag, bucket). compute_kpis kör på en list[dict] med
    # amount_month/amount_ytd per aggregerat konto — vi anropar det per cell.
    bucket_keys = [b.key for b in buckets]
    kpi_meta: dict[str, dict] = {}            # kpi_id → metadata (label_sv, format, ...)
    kpi_by_company: dict[str, dict[str, dict[str, float | None]]] = {}
                                              # kpi_id → company_id → bucket_key → value
    for cid in company_ids_list:
        cid_str = str(cid)
        for bk in bucket_keys:
            # Bygg en list[dict] med {account_id, is_aggregated, amount_month, amount_ytd}
            # där amount_month==amount_ytd (compute_kpis kör båda men vi bryr oss bara om en
            # kolumn — vi använder amount_ytd som "vår bucket" eftersom det inte sign-floppas separat).
            stub_rows = []
            for r in rows:
                if not r["is_aggregated"]:
                    continue
                v = r["by_company"].get(cid_str, {}).get(bk)
                stub_rows.append({
                    "account_id":    r["account_id"],
                    "is_aggregated": True,
                    "amount_month":  v,
                    "amount_ytd":    v,
                })
            kpis_for_cell = compute_kpis(stub_rows)
            for kid, k in kpis_for_cell.items():
                if kid not in kpi_meta:
                    kpi_meta[kid] = {
                        "id":       kid,
                        "label_sv": k["label_sv"],
                        "label_en": k["label_en"],
                        "anchor":   k["anchor"],
                        "format":   k["format"],
                        "emphasis": k["emphasis"],
                    }
                kpi_by_company.setdefault(kid, {}).setdefault(cid_str, {})[bk] = _safe_num(
                    k["amount_ytd"]
                )

    kpis_out = []
    for kid, meta in kpi_meta.items():
        kpis_out.append({**meta, "by_company": kpi_by_company.get(kid, {})})

    return {
        "buckets":        [{"key": b.key, "label": b.label, "start": b.start,
                            "end": b.end, "granularity": b.granularity}
                           for b in buckets],
        "companies":      companies,
        "rows":           rows,
        "kpis":           kpis_out,
        "report_currency": report_currency,
        "scenario":       scenario,
        "granularity":    granularity,
        "period_from":    period_from,
        "period_to":      period_to,
    }


# ----- Counterparties (Norwegian Brreg + sanctions) ---------------------------

@app.get("/api/counterparties/periods")
async def counterparties_periods():
    """Listar perioder med antingen färdig CSV-rapport eller SAF-T-filer redo att köras."""
    return {"periods": counterparty_data.list_available_periods()}


@app.get("/api/counterparties")
async def counterparties_get(
    period: str = Query(..., description="YYYYMM, måste matcha en counterparty_check_*.csv"),
):
    """Returnerar motparter (CSV-rapport) berikat med drilldown per orgnr → bolag."""
    if not (len(period) == 6 and period.isdigit()):
        raise HTTPException(status_code=400, detail=f"Ogiltigt period-format: {period!r}")
    rows = counterparty_data.read_counterparties(period)
    if not rows:
        # Ingen CSV — be användaren köra först
        return {
            "period":       period,
            "rows":         [],
            "csv_exists":   False,
            "message":      "Ingen counterparty_check_{period}.csv finns. Kör en check först.",
        }
    return {
        "period":     period,
        "rows":       rows,
        "csv_exists": True,
        "n_total":    len(rows),
        "n_flagged":  sum(1 for r in rows if r["status"] == "flagged"),
    }


@app.post("/api/counterparties/run")
async def counterparties_run(payload: dict = Body(...)):
    """Trigga check_counterparties.py i bakgrunden. Body: {period, with_sanctions, include_customers}."""
    period           = str(payload.get("period", ""))
    with_sanctions   = bool(payload.get("with_sanctions", False))
    include_customers = bool(payload.get("include_customers", False))
    try:
        return counterparty_runner.start_run(period, with_sanctions, include_customers)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/counterparties/run/status")
async def counterparties_run_status():
    return counterparty_runner.get_status()


# ----- Suppliers ---------------------------------------------------------------

def _parse_int_list(csv: str | None) -> list[int] | None:
    if not csv:
        return None
    try:
        ids = [int(x.strip()) for x in csv.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="Ogiltigt int-listformat")
    return ids or None


def _parse_str_list(csv: str | None) -> list[str] | None:
    if not csv:
        return None
    items = [x.strip() for x in csv.split(",") if x.strip()]
    return items or None


@app.get("/api/suppliers/meta")
async def suppliers_meta(country: str = Query(..., description="Sweden|...")):
    """Metadata för filter: bolag, segment, år, kategorier."""
    with db() as con:
        comp = con.execute(
            """SELECT f.company_id, COALESCE(c.name, f.bolag_label) AS name,
                      f.bolag_label,
                      SUM(f.amount) FILTER (WHERE f.year = (SELECT MAX(year)
                                                           FROM fact_supplier_spend
                                                           WHERE country = ? AND period_kind='FULL')) AS latest_total
               FROM fact_supplier_spend f
               LEFT JOIN dim_company c ON c.company_id = f.company_id
               WHERE f.country = ?
               GROUP BY f.company_id, c.name, f.bolag_label
               ORDER BY name""",
            [country, country],
        ).fetchall()
        years = [
            int(r[0]) for r in con.execute(
                """SELECT DISTINCT year FROM fact_supplier_spend
                   WHERE country = ? AND period_kind='FULL'
                   ORDER BY year""",
                [country],
            ).fetchall()
        ]
        segments = [
            r[0] for r in con.execute(
                """SELECT DISTINCT segment FROM fact_supplier_spend
                   WHERE country = ? AND segment IS NOT NULL
                   ORDER BY segment""",
                [country],
            ).fetchall()
        ]
        kategorier = [
            r[0] for r in con.execute(
                """SELECT DISTINCT kategori FROM fact_supplier_spend
                   WHERE country = ? AND kategori IS NOT NULL
                   ORDER BY kategori""",
                [country],
            ).fetchall()
        ]
        n_total = con.execute(
            "SELECT COUNT(*) FROM fact_supplier_spend WHERE country = ?", [country],
        ).fetchone()[0]
    return {
        "country":  country,
        "n_rows":   int(n_total),
        "years":    years,
        "segments": segments,
        "kategorier": kategorier,
        "companies": [
            {
                "company_id":   int(r[0]) if r[0] is not None else None,
                "name":         _safe_str(r[1]),
                "bolag_label":  _safe_str(r[2]),
                "latest_total": _safe_num(r[3]),
            }
            for r in comp
        ],
    }


def _pivot_to_rows(
    df, key_cols: list[str], years: list[int], compare_year: int | None = None,
) -> list[dict]:
    """df har kolumnerna key_cols + ['year','amount']. Returnerar en rad per nyckel
    med {key_cols, by_year: {year: amount}, total_latest, growth_yoy, share_latest}.

    compare_year (default = max(years)) styr vilket år som används för
    total_latest/growth_yoy/share_latest. growth_yoy = (compare / compare-1) - 1.
    """
    by_key: dict[tuple, dict] = {}
    for r in df.to_dict("records"):
        key = tuple(_safe_str(r.get(c)) for c in key_cols)
        rec = by_key.setdefault(key, {**{c: _safe_str(r.get(c)) for c in key_cols},
                                       "by_year": {}})
        y = int(r["year"])
        rec["by_year"][str(y)] = _safe_num(r["amount"])

    if not years:
        return []
    latest = compare_year if compare_year is not None and compare_year in years else max(years)
    prev = latest - 1 if (latest - 1) in years else None

    rows = list(by_key.values())
    total_latest = sum((r["by_year"].get(str(latest)) or 0.0) for r in rows) or 1.0
    for r in rows:
        l = r["by_year"].get(str(latest))
        p = r["by_year"].get(str(prev)) if prev is not None else None
        r["total_latest"] = _safe_num(l)
        if l is not None and p is not None and p != 0:
            r["growth_yoy"] = (l - p) / abs(p)
        else:
            r["growth_yoy"] = None
        r["share_latest"] = (l / total_latest) if l is not None else None
    rows.sort(key=lambda x: -(x["total_latest"] or 0))
    return rows


@app.get("/api/suppliers/by_supplier")
async def suppliers_by_supplier(
    country: str = Query(..., description="Sweden|..."),
    company_ids: str | None = Query(None, description="kommaseparerad"),
    segments: str | None = Query(None, description="kommaseparerad: Direkt,Indirekt,Interna inköp"),
    include_uncategorized: bool = Query(True),
    compare_year: int | None = Query(None, description="referensår för growth/share"),
):
    """Pivot: per leverantör (förenklat) × år."""
    cids = _parse_int_list(company_ids)
    segs = _parse_str_list(segments)
    sql = SQL_SUP_BY_SUPPLIER.read_text(encoding="utf-8")
    with db() as con:
        years = [int(r[0]) for r in con.execute(
            """SELECT DISTINCT year FROM fact_supplier_spend
               WHERE country = ? AND period_kind='FULL'
               ORDER BY year""", [country],
        ).fetchall()]
        df = con.execute(
            sql,
            [country, cids, cids, segs, segs, include_uncategorized],
        ).df()
    rows = _pivot_to_rows(df, ["supplier_name"], years, compare_year)
    return {"country": country, "years": years, "compare_year": compare_year or (max(years) if years else None), "rows": rows}


@app.get("/api/suppliers/by_category")
async def suppliers_by_category(
    country: str = Query(..., description="Sweden|..."),
    company_ids: str | None = Query(None, description="kommaseparerad"),
    segments: str | None = Query(None, description="kommaseparerad: Direkt,Indirekt,Interna inköp"),
    include_uncategorized: bool = Query(True),
    compare_year: int | None = Query(None, description="referensår för growth/share"),
):
    """Pivot: per kategori (+ segment) × år."""
    cids = _parse_int_list(company_ids)
    segs = _parse_str_list(segments)
    sql = SQL_SUP_BY_CATEGORY.read_text(encoding="utf-8")
    with db() as con:
        years = [int(r[0]) for r in con.execute(
            """SELECT DISTINCT year FROM fact_supplier_spend
               WHERE country = ? AND period_kind='FULL'
               ORDER BY year""", [country],
        ).fetchall()]
        df = con.execute(
            sql,
            [country, cids, cids, segs, segs, include_uncategorized],
        ).df()
    rows = _pivot_to_rows(df, ["kategori", "segment"], years, compare_year)
    return {"country": country, "years": years, "compare_year": compare_year or (max(years) if years else None), "rows": rows}
