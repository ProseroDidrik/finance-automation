"""FastAPI-backend för Finance Reporting GUI.

Kör (dev):
    py -m uvicorn webapp.backend.main:app --reload --port 8000

Endpoints:
    GET /api/companies                         — lista bolag (med country, currency)
    GET /api/periods                           — perioder med data
    GET /api/report/pnl?company_id=X&period=Y  — P&L-rapport (tree + KPIs)
    GET /api/compare/coverage                  — backup vs fact_balances täckning
"""
from __future__ import annotations

import math
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import duckdb
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from webapp.backend.kpi import compute_kpis  # noqa: E402
from webapp.backend.layout import reorder_rows  # noqa: E402
from webapp.backend.period_utils import prev_period, year_start  # noqa: E402

DB_PATH = REPO / "data" / "finance.duckdb"
SQL_PATH = REPO / "webapp" / "backend" / "sql" / "report_pnl.sql"
SQL_COVERAGE = REPO / "webapp" / "backend" / "sql" / "compare_coverage.sql"

# ----- Connection lifecycle ---------------------------------------------------

_con: duckdb.DuckDBPyConnection | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _con
    if not DB_PATH.exists():
        raise RuntimeError(f"DuckDB-warehouset saknas: {DB_PATH}")
    _con = duckdb.connect(str(DB_PATH), read_only=True)
    yield
    if _con is not None:
        _con.close()


def db() -> duckdb.DuckDBPyConnection:
    if _con is None:
        raise RuntimeError("DB inte initierad")
    return _con


# ----- App --------------------------------------------------------------------

app = FastAPI(title="Finance Reporting API", lifespan=lifespan)

# CORS för Vite-dev-server (port 5173)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["GET"],
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


# ----- Endpoints --------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"status": "ok", "db": str(DB_PATH)}


@app.get("/api/companies")
def list_companies():
    """Bolag som har P&L-data i någon period (filtrerade på consolidated)."""
    rows = db().execute(
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
def list_periods(company_id: int | None = Query(None)):
    """Alla perioder med data, eller filtrerat per bolag."""
    if company_id is None:
        rows = db().execute(
            """SELECT period, COUNT(DISTINCT company_id) AS n_companies
               FROM fact_balances GROUP BY period ORDER BY period DESC"""
        ).df().to_dict("records")
    else:
        rows = db().execute(
            """SELECT period FROM fact_balances WHERE company_id = ?
               GROUP BY period ORDER BY period DESC""",
            [company_id],
        ).df().to_dict("records")
    return {"periods": rows}


@app.get("/api/report/pnl")
def pnl_report(
    company_id: int = Query(..., description="dim_company.company_id"),
    period: str = Query(..., description="YYYYMM"),
):
    """P&L-rapport: tree (raw, SIE-konvention) + kpis (post-flip)."""
    try:
        prev = prev_period(period)
        ystart = year_start(period)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    info = db().execute(
        "SELECT company_id, name, country, currency FROM dim_company WHERE company_id = ?",
        [company_id],
    ).fetchone()
    if info is None:
        raise HTTPException(status_code=404, detail=f"Bolag {company_id} hittades inte")

    sql = SQL_PATH.read_text(encoding="utf-8")
    df = db().execute(sql, [company_id, ystart, period, prev, period]).df()

    rows = []
    for r in df.to_dict("records"):
        rows.append({
            "account_id":     _safe_str(r["account_id"]),
            "parent_id":      _safe_str(r.get("parent_id")),
            "label_sv":       _safe_str(r["label_sv"]),
            "label_en":       _safe_str(r["label_en"]),
            "is_aggregated":  bool(r["is_aggregated"]),
            "depth":          int(r["depth"]),
            "account_code":   _safe_str(r.get("account_code")),
            "leaf_label":     _safe_str(r.get("leaf_label")),
            "amount_month":   _safe_num(r.get("amount_month")),
            "amount_ytd":     _safe_num(r.get("amount_ytd")),
            "sort_path":      _safe_str(r["sort_path"]),
        })

    # KPI-beräkning innan sort_path skrivs om (KPI:erna är ändå anchor-baserade)
    kpis_dict = compute_kpis(rows)
    # Sortera om enligt Mercur-ordningen via webapp/config/pnl_layout.yaml
    rows = reorder_rows(rows)
    kpis = []
    for kid, k in kpis_dict.items():
        kpis.append({
            "id":           kid,
            "label_sv":     k["label_sv"],
            "label_en":     k["label_en"],
            "anchor":       k["anchor"],
            "format":       k["format"],
            "emphasis":     k["emphasis"],
            "amount_month": _safe_num(k["amount_month"]),
            "amount_ytd":   _safe_num(k["amount_ytd"]),
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
def compare_coverage():
    """Jämförelse backup_from_mercur vs fact_balances per (bolag, period, källa, scenario)."""
    sql = SQL_COVERAGE.read_text(encoding="utf-8")
    rows = db().execute(sql).df().to_dict("records")
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
