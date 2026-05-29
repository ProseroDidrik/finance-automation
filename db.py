"""Postgres-anslutning och schema för finance-warehouse.

Migrerat från DuckDB → Azure Database for PostgreSQL Flexible Server.

Lokalt: docker compose up -d postgres + sätt DATABASE_URL.
Produktion: App Service injicerar DATABASE_URL från Key Vault.

Star schema: fact_balances + dim_company + dim_period. Verifikat-rader
(SIE/SAF-T) ligger i fact_journal_sie / fact_journal_saft.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
import calendar

import psycopg
from psycopg.rows import dict_row

from shared import load_dotterbolag_full

_REPO_ROOT = Path(__file__).resolve().parent
DOTTERBOLAG_PATH = _REPO_ROOT / "_params" / "Dotterbolagslista.xlsx"

# IMP-lagret: alla auto-import-källor som tillsammans utgör "primär källfil-import".
# Override och delete jobbar mot detta lager (lager-isolering: rör aldrig MAN/IMP_ADJ).
# Per land mappar IMP-koncept till olika source_kind-värden:
IMP_KINDS_BY_COUNTRY = {
    "Sweden":  ("SIE", "SIE_PSALDO", "SIE_VER"),
    "CA":      ("SIE", "SIE_PSALDO", "SIE_VER"),
    "Norway":  ("SAFT", "SIE", "SIE_PSALDO"),  # normalt SAF-T; enstaka bolag SIE
    "Finland": ("IMP",),
    "Denmark": ("IMP",),
    "Germany": ("IMP",),
    "CENTR":   ("IMP",),
}
IMP_KINDS = ("IMP", "SIE", "SIE_PSALDO", "SIE_VER", "SAFT")


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


class Conn:
    """Tunn wrapper kring psycopg.Connection som speglar tidigare DuckDB-API.

    Befintliga loaders kallar ``con.execute(sql, [params]).fetchone()`` och
    ``con.executemany(sql, rows)`` direkt på connection. psycopg använder
    cursor som ett separat objekt — wrappern håller en intern cursor och
    accepterar ``BEGIN``/``COMMIT``/``ROLLBACK`` som SQL-strängar för att
    minimera diff i kallande kod.
    """

    def __init__(self, conn: psycopg.Connection):
        self._conn = conn
        self._cur: psycopg.Cursor | None = None

    def _ensure_cursor(self) -> psycopg.Cursor:
        if self._cur is None or self._cur.closed:
            self._cur = self._conn.cursor()
        return self._cur

    def execute(self, sql: str, params=None) -> "Conn":
        # Hantera transaktionskommandon utan att skicka dem till servern.
        # I psycopg är autocommit-läget connection-level; "BEGIN" i SQL
        # räcker inte för att starta en transaktion, och "COMMIT" som SQL
        # fungerar inte tillförlitligt om autocommit=False. Översätt:
        s = sql.strip().rstrip(";").upper()
        if s == "BEGIN":
            return self
        if s == "COMMIT":
            self._conn.commit()
            return self
        if s == "ROLLBACK":
            self._conn.rollback()
            return self
        cur = self._ensure_cursor()
        if params is None:
            cur.execute(sql)
        else:
            cur.execute(sql, params)
        return self

    def executemany(self, sql: str, rows) -> "Conn":
        cur = self._ensure_cursor()
        cur.executemany(sql, rows)
        return self

    def fetchone(self):
        return self._cur.fetchone() if self._cur is not None else None

    def fetchall(self):
        return self._cur.fetchall() if self._cur is not None else []

    def fetch_dicts(self, sql: str, params=None) -> list[dict]:
        """SELECT → list[dict] via dict_row-cursor. Ersätter tidigare DuckDB-mönster
        ``con.execute(sql, params).df().to_dict("records")`` i webapp-endpoints."""
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()

    def cursor(self) -> psycopg.Cursor:
        """Skapa en NY cursor (oberoende av wrapperns interna)."""
        return self._conn.cursor()

    @property
    def raw(self) -> psycopg.Connection:
        """Underliggande psycopg-connection — t.ex. för pd.read_sql_query."""
        return self._conn

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        if self._cur is not None and not self._cur.closed:
            self._cur.close()
        self._conn.close()

    def close_cursor(self) -> None:
        """Stäng den interna cursorn utan att stänga anslutningen. Används av
        poolade läsvägar där anslutningen återlämnas till poolen, inte stängs."""
        if self._cur is not None and not self._cur.closed:
            self._cur.close()
        self._cur = None

    def __enter__(self) -> "Conn":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self.close()


# T2 (2026-05-25): role-medveten anslutning. ETL och admin har separata
# credentials så loaders aldrig kör som pgadmin (azure_pg_admin, rolbypassrls).
#   role='etl'    → DATABASE_URL_ETL    (etl_writer, DML men ingen DDL)
#                   fallback DATABASE_URL (för lokal dev/bakåtkompat)
#                   fail-fast om current_user är medlem i azure_pg_admin
#   role='admin'  → DATABASE_URL_ADMIN  (pgadmin, för db.py:main schema-init)
#                   fallback DATABASE_URL
#   role='legacy' → DATABASE_URL rakt av (för webapp ConnectionPool; T9-scope)

_ROLE_ENV = {
    "etl":    ("DATABASE_URL_ETL",   "DATABASE_URL"),
    "admin":  ("DATABASE_URL_ADMIN", "DATABASE_URL"),
    "legacy": ("DATABASE_URL",),
}


def _database_url(role: str = "legacy") -> str:
    if role not in _ROLE_ENV:
        raise ValueError(f"okänd role {role!r} (förväntat: etl|admin|legacy)")
    for var in _ROLE_ENV[role]:
        url = os.environ.get(var)
        if url:
            return url
    tried = " eller ".join(_ROLE_ENV[role])
    raise RuntimeError(
        f"{tried} saknas i env (för role={role!r}). Lokalt:\n"
        '  $env:DATABASE_URL = "postgresql://dev:dev@localhost:5432/finance"\n'
        "Lokal Postgres via Docker: docker compose up -d postgres\n"
        "Mot Azure: se db/migrations/RUNBOOK_T2.md för DATABASE_URL_ETL-uppsättning."
    )


def database_url() -> str:
    """Publik accessor för DATABASE_URL — webappens connection pool m.fl.

    Returnerar råa DATABASE_URL (legacy). T9 hanterar webappens egen rollindelning.
    """
    return _database_url("legacy")


def connect(read_only: bool = False, role: str = "etl") -> Conn:
    """Öppna en warehouse-anslutning.

    read_only=True  → autocommit=True (webapp/SELECT-paths håller inga transaktioner).
    read_only=False → autocommit=False (loaders kör explicit BEGIN/COMMIT).
    role='etl'      → fail-fast om current_user är pgadmin/azure_pg_admin-medlem.
                      Default — alla loaders (load_*.py, delete_db.py) ska gå hit.
    role='admin'    → ingen rollkontroll. Bara för db.py:main() / explicit DDL.

    Fail-fast-kollen körs BARA när read_only=False. Read-only-script
    (check_*.py, verify_*.py) kan inte skada data så de undantas — annars
    skulle T2 indirekt kräva DATABASE_URL_ETL även för debug-script som
    historiskt körts med ren DATABASE_URL=admin.
    """
    url = _database_url(role)
    conn = psycopg.connect(url, autocommit=read_only)
    wrapped = Conn(conn)
    if role == "etl" and not read_only:
        _enforce_non_admin(wrapped)
    return wrapped


def _enforce_non_admin(con: Conn) -> None:
    """Avbryt om vi råkar köra skrivande ETL med admin-credentials.

    Använder `pg_has_role` med 'MEMBER'-mode så transitivt medlemskap fångas
    (om någon framtida roll ärver via mellan-roll). Stänger anslutningen
    och raisar RuntimeError så loadern dör snabbt med tydlig text istället
    för att skriva data med fel rättigheter.
    """
    with con.raw.cursor() as cur:
        cur.execute("""
            SELECT current_user,
                   pg_has_role(current_user, 'azure_pg_admin', 'MEMBER')
                OR pg_has_role(current_user, 'pg_write_all_data', 'MEMBER')
                OR current_user = 'pgadmin'
        """)
        user, is_admin = cur.fetchone()
    if is_admin:
        con.close()
        raise RuntimeError(
            f"ETL ansluten som '{user}' som är admin/azure_pg_admin-medlem — förbjudet.\n"
            "Sätt DATABASE_URL_ETL till etl_writer-credentials. Se db/migrations/RUNBOOK_T2.md.\n"
            "Tillfälligt admin-bypass (engångskörning): connect(role='admin')."
        )


SCHEMA_SQL = """
CREATE SEQUENCE IF NOT EXISTS seq_fact_balances START 1;
CREATE SEQUENCE IF NOT EXISTS seq_load_history START 1;
CREATE SEQUENCE IF NOT EXISTS seq_fact_journal_sie START 1;
CREATE SEQUENCE IF NOT EXISTS seq_fact_journal_saft START 1;
CREATE SEQUENCE IF NOT EXISTS seq_dim_exchange_rate START 1;
CREATE SEQUENCE IF NOT EXISTS seq_backup_from_mercur START 1;
CREATE SEQUENCE IF NOT EXISTS seq_fact_personnel START 1;
CREATE SEQUENCE IF NOT EXISTS seq_fact_supplier_spend START 1;
CREATE SEQUENCE IF NOT EXISTS seq_fact_saft_analysis START 1;
CREATE SEQUENCE IF NOT EXISTS seq_fact_sie_analysis START 1;

CREATE TABLE IF NOT EXISTS dim_company (
    company_id           INTEGER PRIMARY KEY,
    name                 TEXT NOT NULL,
    country              TEXT NOT NULL,
    currency             TEXT NOT NULL,
    orgnr                TEXT,
    domain               TEXT,
    kind                 TEXT,
    acquisition_year     INTEGER,
    parent_id            INTEGER,        -- FK dim_company.company_id (konsoliderat moderbolag)
    -- Förvärvsmetadata (Dotterbolagslistan kol K-P)
    closing_date         DATE,
    investment_currency  TEXT,
    ev_sek_m             DOUBLE PRECISION,
    ev_ebitda_ltm        DOUBLE PRECISION,
    ebitda_ltm           DOUBLE PRECISION,
    sales_ltm            DOUBLE PRECISION,
    updated_at           TIMESTAMP NOT NULL
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
    amount          DOUBLE PRECISION NOT NULL,
    currency        TEXT NOT NULL,
    statement_type  TEXT,                  -- 'IS' | 'BS' | NULL
    source_kind     TEXT NOT NULL,         -- 'IMP'|'SIE'|'SIE_PSALDO'|'SIE_VER'|'SAFT'|'MAN'|'IMP_ADJ'|'IB'
    source_file     TEXT NOT NULL,
    row_index       INTEGER,
    scenario        TEXT NOT NULL DEFAULT 'A',  -- 'A' = Actuals | 'B' = Budget
    loaded_at       TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS dim_exchange_rate (
    period      TEXT NOT NULL,
    currency    TEXT NOT NULL,
    rate_type   TEXT NOT NULL,
    rate        DOUBLE PRECISION NOT NULL,
    loaded_at   TIMESTAMP NOT NULL,
    PRIMARY KEY (period, currency, rate_type)
);

CREATE INDEX IF NOT EXISTS idx_fb_company_period ON fact_balances(company_id, period);
CREATE INDEX IF NOT EXISTS idx_fb_period         ON fact_balances(period);
CREATE INDEX IF NOT EXISTS idx_fb_account        ON fact_balances(account_code);
CREATE INDEX IF NOT EXISTS idx_fb_idem
    ON fact_balances(company_id, period, source_kind, source_file);

CREATE TABLE IF NOT EXISTS fact_journal_sie (
    id              BIGINT PRIMARY KEY DEFAULT nextval('seq_fact_journal_sie'),
    company_id      INTEGER NOT NULL,
    period          TEXT NOT NULL,
    series          TEXT,
    voucher_number  TEXT NOT NULL,
    voucher_date    DATE NOT NULL,
    voucher_text    TEXT,
    line_no         INTEGER NOT NULL,
    account_code    TEXT NOT NULL,
    account_name    TEXT,
    amount          DOUBLE PRECISION NOT NULL,
    transaction_text TEXT,
    quantity        DOUBLE PRECISION,
    currency        TEXT NOT NULL,
    source_file     TEXT NOT NULL,
    loaded_at       TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fjs_company_period ON fact_journal_sie(company_id, period);
CREATE INDEX IF NOT EXISTS idx_fjs_voucher        ON fact_journal_sie(company_id, series, voucher_number);
CREATE INDEX IF NOT EXISTS idx_fjs_account        ON fact_journal_sie(account_code);
CREATE INDEX IF NOT EXISTS idx_fjs_period         ON fact_journal_sie(period);

CREATE TABLE IF NOT EXISTS fact_journal_saft (
    id                 BIGINT PRIMARY KEY DEFAULT nextval('seq_fact_journal_saft'),
    company_id         INTEGER NOT NULL,
    period             TEXT NOT NULL,
    journal_id         TEXT,
    journal_description TEXT,
    transaction_id     TEXT NOT NULL,
    transaction_date   DATE,
    transaction_description TEXT,
    line_no            INTEGER NOT NULL,
    record_id          TEXT,
    account_code       TEXT NOT NULL,
    debit_amount       DOUBLE PRECISION,
    credit_amount      DOUBLE PRECISION,
    amount             DOUBLE PRECISION NOT NULL,
    line_description   TEXT,
    currency           TEXT NOT NULL,
    source_file        TEXT NOT NULL,
    loaded_at          TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fjsaft_company_period ON fact_journal_saft(company_id, period);
CREATE INDEX IF NOT EXISTS idx_fjsaft_transaction    ON fact_journal_saft(company_id, transaction_id);
CREATE INDEX IF NOT EXISTS idx_fjsaft_account        ON fact_journal_saft(account_code);
CREATE INDEX IF NOT EXISTS idx_fjsaft_period         ON fact_journal_saft(period);

-- Dimensioner (SAF-T Analysis nu; SIE #DIM/#OBJEKT framtida via source_format).
-- dim_* är best-effort namnslagning ur MasterFiles/AnalysisTypeTable; ingen FK
-- fakta→dim (Line.Analysis kan referera koder utan motsvarande dim-rad).
CREATE TABLE IF NOT EXISTS dim_analysis_type (
    company_id      INTEGER NOT NULL,
    source_format   TEXT NOT NULL,         -- 'SAFT' | 'SIE'
    analysis_type   TEXT NOT NULL,
    description     TEXT,
    loaded_at       TIMESTAMP NOT NULL,
    PRIMARY KEY (company_id, source_format, analysis_type)
);

CREATE TABLE IF NOT EXISTS dim_analysis_member (
    company_id      INTEGER NOT NULL,
    source_format   TEXT NOT NULL,
    analysis_type   TEXT NOT NULL,
    analysis_id     TEXT NOT NULL,
    description     TEXT,
    loaded_at       TIMESTAMP NOT NULL,
    PRIMARY KEY (company_id, source_format, analysis_type, analysis_id)
);

-- En rad per (journallinje × Analysis-block). period = ValueDate-härledd per
-- linje (= journalens period); amount = linjens belopp (debit-credit),
-- MÅNADSRÖRELSE. Multi-axel upprepar beloppet → SUM aldrig över analysis_type.
CREATE TABLE IF NOT EXISTS fact_saft_analysis (
    id              BIGINT PRIMARY KEY DEFAULT nextval('seq_fact_saft_analysis'),
    company_id      INTEGER NOT NULL,
    period          TEXT NOT NULL,
    transaction_id  TEXT,
    line_no         INTEGER NOT NULL,
    record_id       TEXT,
    account_code    TEXT NOT NULL,
    analysis_type   TEXT NOT NULL,
    analysis_id     TEXT NOT NULL,
    amount          DOUBLE PRECISION NOT NULL,
    currency        TEXT NOT NULL,
    source_file     TEXT NOT NULL,
    loaded_at       TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fsa_company_period ON fact_saft_analysis(company_id, period);
CREATE INDEX IF NOT EXISTS idx_fsa_type_member    ON fact_saft_analysis(company_id, analysis_type, analysis_id);
CREATE INDEX IF NOT EXISTS idx_fsa_account        ON fact_saft_analysis(account_code);
CREATE INDEX IF NOT EXISTS idx_fsa_period         ON fact_saft_analysis(period);

-- SIE-dimensioner: en rad per (#TRANS-linje × dim-par). period = verifikatets
-- månad (= fact_journal_sie). amount = #TRANS-beloppet, MÅNADSRÖRELSE.
-- Multi-dim upprepar beloppet → SUM aldrig över analysis_type. Delar
-- dim_analysis_type/_member med SAF-T via source_format='SIE'.
CREATE TABLE IF NOT EXISTS fact_sie_analysis (
    id              BIGINT PRIMARY KEY DEFAULT nextval('seq_fact_sie_analysis'),
    company_id      INTEGER NOT NULL,
    period          TEXT NOT NULL,
    series          TEXT,
    voucher_number  TEXT,
    line_no         INTEGER NOT NULL,
    account_code    TEXT NOT NULL,
    analysis_type   TEXT NOT NULL,
    analysis_id     TEXT NOT NULL,
    amount          DOUBLE PRECISION NOT NULL,
    currency        TEXT NOT NULL,
    source_file     TEXT NOT NULL,
    loaded_at       TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fsie_company_period ON fact_sie_analysis(company_id, period);
CREATE INDEX IF NOT EXISTS idx_fsie_type_member    ON fact_sie_analysis(company_id, analysis_type, analysis_id);
CREATE INDEX IF NOT EXISTS idx_fsie_account        ON fact_sie_analysis(account_code);
CREATE INDEX IF NOT EXISTS idx_fsie_period         ON fact_sie_analysis(period);

CREATE TABLE IF NOT EXISTS dim_account_map (
    account_id      TEXT PRIMARY KEY,
    description     TEXT,
    description_en  TEXT,
    is_aggregated   BOOLEAN NOT NULL,
    parent_id       TEXT,
    source          TEXT,
    company_id      INTEGER,
    account_code    TEXT,
    loaded_at       TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dam_company  ON dim_account_map(company_id, account_code);
CREATE INDEX IF NOT EXISTS idx_dam_parent   ON dim_account_map(parent_id);

CREATE TABLE IF NOT EXISTS backup_from_mercur (
    id           BIGINT PRIMARY KEY DEFAULT nextval('seq_backup_from_mercur'),
    company_id   INTEGER NOT NULL,
    period       TEXT NOT NULL,
    account_code TEXT NOT NULL,
    account_name TEXT,
    amount       DOUBLE PRECISION NOT NULL,
    currency     TEXT NOT NULL,
    source_kind  TEXT NOT NULL,
    scenario     TEXT NOT NULL DEFAULT 'A',
    source_file  TEXT NOT NULL,
    row_index    INTEGER,
    loaded_at    TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bfm_company_period ON backup_from_mercur(company_id, period);
CREATE INDEX IF NOT EXISTS idx_bfm_idem           ON backup_from_mercur(company_id, period, source_kind, scenario);

CREATE TABLE IF NOT EXISTS fact_personnel (
    id                  BIGINT PRIMARY KEY DEFAULT nextval('seq_fact_personnel'),
    country             TEXT NOT NULL,
    company_id          INTEGER NOT NULL,
    employee_name       TEXT NOT NULL,
    title               TEXT,
    birth_date          DATE,
    employed_from       DATE,
    employed_to         DATE,
    termination_reason  TEXT,
    employment_pct      DOUBLE PRECISION,
    productivity        DOUBLE PRECISION,
    billable_pct        DOUBLE PRECISION,
    gender              TEXT,
    category            TEXT,
    salary_local        DOUBLE PRECISION,
    location            TEXT,
    apprenticeship_end  DATE,
    pension_apprentice  TEXT,
    snapshot_date       DATE NOT NULL,
    source_file         TEXT NOT NULL,
    loaded_at           TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fp_country_company ON fact_personnel(country, company_id);
CREATE INDEX IF NOT EXISTS idx_fp_company         ON fact_personnel(company_id);

CREATE TABLE IF NOT EXISTS dim_supplier_register (
    country         TEXT NOT NULL,
    levprefix       TEXT NOT NULL,
    supplier_name   TEXT,
    kategori        TEXT,
    segment         TEXT,
    source_file     TEXT NOT NULL,
    loaded_at       TIMESTAMP NOT NULL,
    PRIMARY KEY (country, levprefix)
);

CREATE INDEX IF NOT EXISTS idx_dsr_country_supplier ON dim_supplier_register(country, supplier_name);
CREATE INDEX IF NOT EXISTS idx_dsr_country_kategori ON dim_supplier_register(country, kategori);

CREATE TABLE IF NOT EXISTS fact_supplier_spend (
    id              BIGINT PRIMARY KEY DEFAULT nextval('seq_fact_supplier_spend'),
    country         TEXT NOT NULL,
    company_id      INTEGER,
    bolag_label     TEXT NOT NULL,
    lev_nr          TEXT,
    namn            TEXT,
    levprefix       TEXT,
    supplier_name   TEXT,
    kategori        TEXT,
    segment         TEXT,
    year            INTEGER NOT NULL,
    period_kind     TEXT NOT NULL,
    amount          DOUBLE PRECISION NOT NULL,
    currency        TEXT NOT NULL,
    source_file     TEXT NOT NULL,
    loaded_at       TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fss_country_year     ON fact_supplier_spend(country, year);
CREATE INDEX IF NOT EXISTS idx_fss_company          ON fact_supplier_spend(company_id);
CREATE INDEX IF NOT EXISTS idx_fss_levprefix        ON fact_supplier_spend(country, levprefix);

CREATE TABLE IF NOT EXISTS load_history (
    id                       BIGINT PRIMARY KEY DEFAULT nextval('seq_load_history'),
    company_id               INTEGER,
    period                   TEXT,
    source_kind              TEXT,
    source_file              TEXT,
    rows_loaded              INTEGER,
    sum_amount               DOUBLE PRECISION,
    statement_type_present   BOOLEAN,
    status                   TEXT,
    message                  TEXT,
    loaded_at                TIMESTAMP NOT NULL
);
"""


def init_schema(con: Conn) -> None:
    """Create tables/indexes if missing. Idempotent."""
    con.execute(SCHEMA_SQL)
    con.commit()
    _migrate(con)
    con.commit()


def _migrate(con: Conn) -> None:
    """Add columns introduced after initial schema. Safe to run repeatedly."""
    # source_kind 'INL' (FI/DK/DE Excel-import) bytte namn till 'IMP' eftersom
    # det konceptuellt är samma lager som Mercur-historikens IMP. Idempotent.
    con.execute("UPDATE fact_balances SET source_kind = 'IMP' WHERE source_kind = 'INL'")
    con.execute("UPDATE load_history  SET source_kind = 'IMP' WHERE source_kind = 'INL'")

    fb_cols = {
        row[0]
        for row in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'fact_balances'"
        ).fetchall()
    }
    if "scenario" not in fb_cols:
        con.execute("ALTER TABLE fact_balances ADD COLUMN scenario TEXT DEFAULT 'A'")
        con.execute("UPDATE fact_balances SET scenario = 'A' WHERE scenario IS NULL")

    dc_cols = {
        row[0]
        for row in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'dim_company'"
        ).fetchall()
    }
    if "acquisition_year" not in dc_cols:
        con.execute("ALTER TABLE dim_company ADD COLUMN acquisition_year INTEGER")
    if "parent_id" not in dc_cols:
        con.execute("ALTER TABLE dim_company ADD COLUMN parent_id INTEGER")
    if "closing_date" not in dc_cols:
        con.execute("ALTER TABLE dim_company ADD COLUMN closing_date DATE")
    if "investment_currency" not in dc_cols:
        con.execute("ALTER TABLE dim_company ADD COLUMN investment_currency TEXT")
    if "ev_sek_m" not in dc_cols:
        con.execute("ALTER TABLE dim_company ADD COLUMN ev_sek_m DOUBLE PRECISION")
    if "ev_ebitda_ltm" not in dc_cols:
        con.execute("ALTER TABLE dim_company ADD COLUMN ev_ebitda_ltm DOUBLE PRECISION")
    if "ebitda_ltm" not in dc_cols:
        con.execute("ALTER TABLE dim_company ADD COLUMN ebitda_ltm DOUBLE PRECISION")
    if "sales_ltm" not in dc_cols:
        con.execute("ALTER TABLE dim_company ADD COLUMN sales_ltm DOUBLE PRECISION")


def sync_dim_company(con: Conn) -> int:
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
            continue
        rows.append((
            bid,
            info.get("name") or "",
            country,
            currency,
            info.get("orgnr") or None,
            info.get("domain") or None,
            info.get("kind") or None,
            info.get("acquisition_year"),
            info.get("parent_id"),
            info.get("closing_date"),
            info.get("investment_currency"),
            info.get("ev_sek_m"),
            info.get("ev_ebitda_ltm"),
            info.get("ebitda_ltm"),
            info.get("sales_ltm"),
            now,
        ))

    con.execute("BEGIN")
    try:
        con.execute("DELETE FROM dim_company")
        con.executemany(
            """INSERT INTO dim_company
               (company_id, name, country, currency, orgnr, domain, kind,
                acquisition_year, parent_id,
                closing_date, investment_currency,
                ev_sek_m, ev_ebitda_ltm, ebitda_ltm, sales_ltm,
                updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            rows,
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return len(rows)


def sync_dim_period(con: Conn, periods: list[str]) -> int:
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
        """INSERT INTO dim_period
           (period, year, month, quarter, period_start, period_end)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (period) DO NOTHING""",
        rows,
    )
    return len(rows)


def upsert_dim_analysis(con: Conn, type_rows: list[tuple],
                        member_rows: list[tuple]) -> None:
    """Upserta dimensionsaxlar + medlemmar till dim_analysis_type/_member.

    Källagnostisk, delad mellan SIE- och SAF-T-loaders (tidigare dupliderad SQL
    på fyra ställen). Körs INOM anroparens transaktion — ingen egen BEGIN/COMMIT.
    type_rows/member_rows kommer från {sie_dim,dim}_analysis_rows och måste ha
    kolumnordningen nedan. Best-effort namnslagning: ON CONFLICT uppdaterar
    description + loaded_at. Tomma listor = no-op (rör inte con)."""
    if type_rows:
        con.executemany(
            """INSERT INTO dim_analysis_type
               (company_id, source_format, analysis_type, description, loaded_at)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (company_id, source_format, analysis_type)
               DO UPDATE SET description = EXCLUDED.description,
                             loaded_at = EXCLUDED.loaded_at""",
            type_rows)
    if member_rows:
        con.executemany(
            """INSERT INTO dim_analysis_member
               (company_id, source_format, analysis_type, analysis_id,
                description, loaded_at)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (company_id, source_format, analysis_type, analysis_id)
               DO UPDATE SET description = EXCLUDED.description,
                             loaded_at = EXCLUDED.loaded_at""",
            member_rows)


def relpath_from_base(path: Path, base: Path) -> str:
    """Return path relativ till base, eller absolut sträng om utanför base."""
    try:
        return str(Path(path).resolve().relative_to(Path(base).resolve())).replace("\\", "/")
    except ValueError:
        return str(Path(path).resolve()).replace("\\", "/")


def main() -> None:
    """CLI: initiera schema + synka dim_company. `py db.py`

    Schema-init kräver DDL → kör som admin (DATABASE_URL_ADMIN > DATABASE_URL).
    Loaders ska istället gå via connect() default (role='etl', etl_writer).
    """
    with connect(role="admin") as con:
        init_schema(con)
        n = sync_dim_company(con)
        print(f"[OK]     dim_company  {n} bolag synkade  ({_database_url('admin')})")


if __name__ == "__main__":
    main()
