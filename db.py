"""DuckDB-anslutning och schema för finance-warehouse.

Lokalt under testperioden: data/finance.duckdb i repo-roten.
Star schema: fact_balances + dim_company + dim_period.
Verifikat-rader (SIE/SAF-T) får senare en separat fact_journal.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import calendar

import duckdb

from shared import load_dotterbolag_full

_REPO_ROOT = Path(__file__).resolve().parent
_DATA_DIR = _REPO_ROOT / "data"
DB_PATH = _DATA_DIR / "finance.duckdb"
DOTTERBOLAG_PATH = _REPO_ROOT / "_params" / "Dotterbolagslista.xlsx"

COUNTRY_CURRENCY = {
    "Sweden": "SEK",
    "Norway": "NOK",
    "Denmark": "DKK",
    "Finland": "EUR",
    "Germany": "EUR",
    # CENTR = centrala/koncerngemensamma bolag (Prosero Security Oy/GmbH).
    # Default EUR; korrigera manuellt om något CENTR-bolag inte är euro-baserat.
    "CENTR": "EUR",
    # CA = bolag med svenskt orgnr men annan koncernklassning (49, 162).
    "CA": "SEK",
}


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open the warehouse DB; create the data dir on first run."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(DB_PATH), read_only=read_only)


SCHEMA_SQL = """
CREATE SEQUENCE IF NOT EXISTS seq_fact_balances START 1;
CREATE SEQUENCE IF NOT EXISTS seq_load_history START 1;

CREATE TABLE IF NOT EXISTS dim_company (
    company_id   INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    country      TEXT NOT NULL,
    currency     TEXT NOT NULL,
    orgnr        TEXT,
    domain       TEXT,
    kind         TEXT,
    updated_at   TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS dim_period (
    period       TEXT PRIMARY KEY,
    year         INTEGER NOT NULL,
    month        INTEGER NOT NULL,
    quarter      INTEGER NOT NULL,
    period_start DATE NOT NULL,
    period_end   DATE NOT NULL
);

CREATE TABLE IF NOT EXISTS fact_balances (
    id              BIGINT PRIMARY KEY DEFAULT nextval('seq_fact_balances'),
    company_id      INTEGER NOT NULL,
    period          TEXT NOT NULL,
    period_type     TEXT NOT NULL,        -- 'monthly' | 'ytd'
    account_code    TEXT NOT NULL,
    account_name    TEXT,
    amount          DOUBLE NOT NULL,
    currency        TEXT NOT NULL,
    statement_type  TEXT,                  -- 'IS' | 'BS' | NULL
    source_kind     TEXT NOT NULL,         -- 'INL' | 'SIE' | 'SAFT'
    source_file     TEXT NOT NULL,         -- relativ till base_path
    row_index       INTEGER,
    loaded_at       TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fb_company_period ON fact_balances(company_id, period);
CREATE INDEX IF NOT EXISTS idx_fb_period         ON fact_balances(period);
CREATE INDEX IF NOT EXISTS idx_fb_account        ON fact_balances(account_code);
CREATE INDEX IF NOT EXISTS idx_fb_idem
    ON fact_balances(company_id, period, source_kind, source_file);

CREATE TABLE IF NOT EXISTS load_history (
    id                       BIGINT PRIMARY KEY DEFAULT nextval('seq_load_history'),
    company_id               INTEGER,
    period                   TEXT,
    source_kind              TEXT,
    source_file              TEXT,
    rows_loaded              INTEGER,
    sum_amount               DOUBLE,
    statement_type_present   BOOLEAN,
    status                   TEXT,                -- 'ok' | 'warn' | 'error'
    message                  TEXT,
    loaded_at                TIMESTAMP NOT NULL
);
"""


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create tables/indexes if missing. Idempotent."""
    con.execute(SCHEMA_SQL)


def sync_dim_company(con: duckdb.DuckDBPyConnection) -> int:
    """Upsert dim_company from Dotterbolagslistan. Returns row count written."""
    if not DOTTERBOLAG_PATH.exists():
        raise FileNotFoundError(
            f"Dotterbolagslistan saknas: {DOTTERBOLAG_PATH}. "
            "Kontrollera att _params/Dotterbolagslista.xlsx finns."
        )
    bolags = load_dotterbolag_full(DOTTERBOLAG_PATH)
    now = datetime.now()
    rows = []
    for bid, info in bolags.items():
        country = info.get("country") or ""
        currency = COUNTRY_CURRENCY.get(country, "")
        if not currency:
            # Saknad/ovanlig country → hoppa över; konsoliderade rader kan ha tomt land.
            continue
        rows.append((
            bid,
            info.get("name") or "",
            country,
            currency,
            info.get("orgnr") or None,
            info.get("domain") or None,
            info.get("kind") or None,
            now,
        ))

    con.execute("BEGIN")
    try:
        con.execute("DELETE FROM dim_company")
        con.executemany(
            """INSERT INTO dim_company
               (company_id, name, country, currency, orgnr, domain, kind, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return len(rows)


def sync_dim_period(con: duckdb.DuckDBPyConnection, periods: list[str]) -> int:
    """Insert any missing 'YYYYMM' periods into dim_period."""
    rows = []
    for p in sorted(set(periods)):
        if len(p) != 6 or not p.isdigit():
            continue
        year = int(p[:4])
        month = int(p[4:])
        if not (1 <= month <= 12):
            continue
        last_day = calendar.monthrange(year, month)[1]
        rows.append((
            p,
            year,
            month,
            (month - 1) // 3 + 1,
            date(year, month, 1),
            date(year, month, last_day),
        ))
    if not rows:
        return 0
    con.executemany(
        """INSERT OR IGNORE INTO dim_period
           (period, year, month, quarter, period_start, period_end)
           VALUES (?, ?, ?, ?, ?, ?)""",
        rows,
    )
    return len(rows)


def relpath_from_base(path: Path, base: Path) -> str:
    """Return path relativ till base, eller absolut sträng om utanför base."""
    try:
        return str(Path(path).resolve().relative_to(Path(base).resolve())).replace("\\", "/")
    except ValueError:
        return str(Path(path).resolve()).replace("\\", "/")


def main() -> None:
    """CLI: initiera schema + synka dim_company. `py db.py`"""
    con = connect()
    try:
        init_schema(con)
        n = sync_dim_company(con)
        print(f"[OK]     dim_company  {n} bolag synkade  ({DB_PATH})")
    finally:
        con.close()


if __name__ == "__main__":
    main()
