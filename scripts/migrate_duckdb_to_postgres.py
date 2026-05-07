"""Engångsmigration: DuckDB-fil → Azure Postgres Flexible Server.

Flöde:
  1. Läs data/finance.duckdb (läsläge).
  2. Anslut till Postgres via DATABASE_URL (samma env-var som db.py).
  3. Kör db.init_schema() mot Postgres (idempotent).
  4. För varje tabell i topologisk ordning: stream rader via psycopg COPY FROM STDIN.
  5. Sätt sequence-värden så framtida INSERT:s inte krockar.
  6. (--verify) Jämför COUNT(*) per tabell + SUM(amount) per
     (company_id, period, source_kind) i fact_balances.

Kör (lokalt mot lokal Postgres för test):
  $env:DATABASE_URL = "postgresql://dev:dev@localhost:5432/finance"
  py scripts/migrate_duckdb_to_postgres.py --verify

Kör (mot Azure Postgres):
  $env:DATABASE_URL = "postgresql://admin@finance-pg:<pwd>@finance-pg.postgres.database.azure.com:5432/finance?sslmode=require"
  py scripts/migrate_duckdb_to_postgres.py --verify
"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import date, datetime
from pathlib import Path

import duckdb
import psycopg

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import db  # noqa: E402

DUCKDB_PATH = REPO / "data" / "finance.duckdb"

# Topologisk ordning: dim-tabeller först, sedan facts. Inga FK-constraints i
# schemat så ordning spelar bara roll för läsbarhet/dependency-resonemang.
TABLES: list[tuple[str, list[str], str | None]] = [
    # (tabellnamn, kolumner, sekvens som ska sättas till MAX(id) eller None)
    ("dim_period", [
        "period", "year", "month", "quarter", "period_start", "period_end",
    ], None),
    ("dim_company", [
        "company_id", "name", "country", "currency", "orgnr", "domain", "kind",
        "acquisition_year", "parent_id",
        "closing_date", "investment_currency",
        "ev_sek_m", "ev_ebitda_ltm", "ebitda_ltm", "sales_ltm",
        "updated_at",
    ], None),
    ("dim_account_map", [
        "account_id", "description", "description_en", "is_aggregated",
        "parent_id", "source", "company_id", "account_code", "loaded_at",
    ], None),
    ("dim_exchange_rate", [
        "period", "currency", "rate_type", "rate", "loaded_at",
    ], None),
    ("dim_supplier_register", [
        "country", "levprefix", "supplier_name", "kategori", "segment",
        "source_file", "loaded_at",
    ], None),
    ("fact_balances", [
        "id", "company_id", "period", "period_type", "account_code",
        "account_name", "amount", "currency", "statement_type", "source_kind",
        "source_file", "row_index", "scenario", "loaded_at",
    ], "seq_fact_balances"),
    ("fact_journal_sie", [
        "id", "company_id", "period", "series", "voucher_number",
        "voucher_date", "voucher_text", "line_no", "account_code",
        "account_name", "amount", "transaction_text", "quantity", "currency",
        "source_file", "loaded_at",
    ], "seq_fact_journal_sie"),
    ("fact_journal_saft", [
        "id", "company_id", "period", "journal_id", "journal_description",
        "transaction_id", "transaction_date", "transaction_description",
        "line_no", "record_id", "account_code", "debit_amount",
        "credit_amount", "amount", "line_description", "currency",
        "source_file", "loaded_at",
    ], "seq_fact_journal_saft"),
    ("fact_personnel", [
        "id", "country", "company_id", "employee_name", "title", "birth_date",
        "employed_from", "employed_to", "termination_reason", "employment_pct",
        "productivity", "billable_pct", "gender", "category", "salary_local",
        "location", "apprenticeship_end", "pension_apprentice",
        "snapshot_date", "source_file", "loaded_at",
    ], "seq_fact_personnel"),
    ("fact_supplier_spend", [
        "id", "country", "company_id", "bolag_label", "lev_nr", "namn",
        "levprefix", "supplier_name", "kategori", "segment", "year",
        "period_kind", "amount", "currency", "source_file", "loaded_at",
    ], "seq_fact_supplier_spend"),
    ("backup_from_mercur", [
        "id", "company_id", "period", "account_code", "account_name", "amount",
        "currency", "source_kind", "scenario", "source_file", "row_index",
        "loaded_at",
    ], "seq_backup_from_mercur"),
    ("load_history", [
        "id", "company_id", "period", "source_kind", "source_file",
        "rows_loaded", "sum_amount", "statement_type_present", "status",
        "message", "loaded_at",
    ], "seq_load_history"),
]


def _clean_value(v):
    """Konvertera DuckDB-värden till psycopg-vänliga typer.

    NaN/inf → None. pd.Timestamp → datetime/date. DuckDB returnerar redan
    Python-native för de flesta typer, men floats kan vara NaN.
    """
    if v is None:
        return None
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    return v


def copy_table(duck: duckdb.DuckDBPyConnection,
               pg: psycopg.Connection,
               table: str,
               cols: list[str]) -> int:
    """Stream rader DuckDB → Postgres via COPY FROM STDIN. Returnerar antal."""
    sql_select = f"SELECT {', '.join(cols)} FROM {table}"
    rows = duck.execute(sql_select).fetchall()
    if not rows:
        return 0
    cols_sql = ", ".join(cols)
    copy_sql = f"COPY {table} ({cols_sql}) FROM STDIN"
    with pg.cursor() as cur:
        with cur.copy(copy_sql) as copy:
            for row in rows:
                copy.write_row(tuple(_clean_value(v) for v in row))
    return len(rows)


def reset_sequence(pg: psycopg.Connection, table: str, seq: str) -> int:
    """Sätt sequence till MAX(id) + 1 så framtida INSERT:s inte kollidera.

    Postgres setval(seq, n, True) gör att nästa nextval returnerar n+1.
    """
    with pg.cursor() as cur:
        cur.execute(f"SELECT MAX(id) FROM {table}")
        max_id = cur.fetchone()[0] or 0
        cur.execute(f"SELECT setval(%s, %s, TRUE)", (seq, max(max_id, 1)))
        return max_id


def verify(duck: duckdb.DuckDBPyConnection, pg: psycopg.Connection) -> bool:
    """Sanity-check: COUNT per tabell + SUM(amount) per (bolag, period, kind)
    i fact_balances. Returnerar True om allt matchar."""
    ok = True
    print("\n=== Verifiering ===")
    for table, _cols, _seq in TABLES:
        d_n = duck.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        with pg.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            p_n = cur.fetchone()[0]
        marker = "OK " if d_n == p_n else "DIFF"
        print(f"  {marker}  {table:<25}  duck={d_n:>10}  pg={p_n:>10}")
        if d_n != p_n:
            ok = False

    # Detaljerad jämförelse för fact_balances:
    d_sums = duck.execute(
        "SELECT company_id, period, source_kind, SUM(amount) "
        "FROM fact_balances GROUP BY 1,2,3"
    ).fetchall()
    d_map = {(c, p, k): float(s or 0) for c, p, k, s in d_sums}

    with pg.cursor() as cur:
        cur.execute(
            "SELECT company_id, period, source_kind, SUM(amount) "
            "FROM fact_balances GROUP BY 1,2,3"
        )
        p_map = {(c, p, k): float(s or 0) for c, p, k, s in cur.fetchall()}

    diffs = 0
    for key, d_sum in d_map.items():
        p_sum = p_map.get(key)
        if p_sum is None or abs(d_sum - p_sum) > 0.01:
            diffs += 1
            if diffs <= 10:
                print(f"  DIFF  fact_balances {key}  duck={d_sum:.2f}  pg={p_sum}")
    if diffs:
        print(f"  -> {diffs} avvikande grupper i fact_balances")
        ok = False
    else:
        print(f"  OK   fact_balances SUM matchar för alla {len(d_map)} grupper")

    return ok


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verify", action="store_true",
                    help="Jämför COUNT/SUM efter migration. Avbryt på mismatch.")
    ap.add_argument("--skip-init", action="store_true",
                    help="Hoppa över init_schema (om Postgres redan har schema).")
    ap.add_argument("--truncate", action="store_true",
                    help="TRUNCATE alla tabeller innan kopiering. Idempotent re-run.")
    args = ap.parse_args()

    if not DUCKDB_PATH.exists():
        sys.exit(f"DuckDB-fil saknas: {DUCKDB_PATH}")

    pg_url = db._database_url()
    print(f"DuckDB: {DUCKDB_PATH}")
    print(f"Postgres: {pg_url.split('@')[-1] if '@' in pg_url else pg_url}")

    duck = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    pg = psycopg.connect(pg_url, autocommit=False)
    try:
        if not args.skip_init:
            print("Initierar schema i Postgres...")
            # Wrappa pg i en Conn UTAN context-manager — vi vill inte stänga
            # den underliggande connection:en mellan steg. init_schema commit:ar
            # internt så pg-handtaget är konsistent när vi går vidare.
            wrapper = db.Conn(pg)
            db.init_schema(wrapper)

        if args.truncate:
            print("TRUNCATE alla migrationstabeller...")
            with pg.cursor() as cur:
                names = ", ".join(t for t, _, _ in TABLES)
                cur.execute(f"TRUNCATE {names} RESTART IDENTITY")
            pg.commit()

        total = 0
        for table, cols, seq in TABLES:
            n = copy_table(duck, pg, table, cols)
            pg.commit()
            print(f"  COPY {table:<25}  {n:>8} rader")
            total += n
            if seq is not None and n > 0:
                max_id = reset_sequence(pg, table, seq)
                pg.commit()
                print(f"    setval({seq}, {max_id})")

        print(f"\nTotalt: {total} rader migrerade.")

        if args.verify:
            if not verify(duck, pg):
                sys.exit("VERIFIERING MISSLYCKADES — diff mellan DuckDB och Postgres.")
            print("\nVerifiering OK.")
    finally:
        duck.close()
        pg.close()


if __name__ == "__main__":
    main()
