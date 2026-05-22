"""Smoke-test täckningssidans SQL-filer mot Azure Postgres före deploy.

Kör båda SQL-filerna med samma wrapping och parametrar som main.py gör i
runtime, så psycopg-side issues (t.ex. %-placeholder-scan i kommentarer)
fångas innan vi committar.

Användning:
    # Default: kör båda täcknings-SQL-filerna mot DATABASE_URL
    py scripts/smoke_test_sql.py

    # En specifik fil (utan wrapping):
    py scripts/smoke_test_sql.py --file webapp/backend/sql/report_pnl.sql --param 32 --param 202604

Setup (från memory reference_local_dev_setup.md):
    $env:DATABASE_URL = (az keyvault secret show --vault-name kv-finauto-6427 --name database-url --query value -o tsv)

Exit-kod 0 = alla tester OK, 1 = minst ett fel.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import psycopg

REPO = Path(__file__).resolve().parent.parent
SQL_DIR = REPO / "webapp" / "backend" / "sql"


def _connect() -> psycopg.Connection:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit(
            "ERROR: DATABASE_URL saknas. Sätt med:\n"
            "  $env:DATABASE_URL = (az keyvault secret show --vault-name kv-finauto-6427 "
            "--name database-url --query value -o tsv)"
        )
    return psycopg.connect(url)


def _run(name: str, sql: str, params: tuple, limit: int = 5) -> bool:
    """Kör SQL med params, return True vid framgång."""
    print(f"\n=== {name} ===")
    print(f"Params: {params}")
    t0 = time.time()
    try:
        with _connect() as con, con.cursor() as cur:
            # 120 s — smoke-testet ska verifiera att SQL:en är giltig och kör,
            # inte fälla korrekta-men-tunga queries. compare_coverage tar ~34 s
            # på Burstable-tiern (Fas 1-mål att få ned); en snålare gräns gav
            # falsk FAIL.
            cur.execute("SET statement_timeout = 120000")
            cur.execute(sql, params)
            rows = cur.fetchmany(limit)
            elapsed = int((time.time() - t0) * 1000)
            print(f"OK: {len(rows)} rader på {elapsed}ms")
            if rows and cur.description:
                cols = [d.name for d in cur.description]
                print(f"Kolumner: {', '.join(cols[:8])}{' ...' if len(cols) > 8 else ''}")
            return True
    except psycopg.errors.SyntaxError as e:
        print(f"FAIL (SQL syntax): {e}")
        return False
    except psycopg.ProgrammingError as e:
        msg = str(e)
        print(f"FAIL (programming): {msg}")
        if "%" in sql and ("argument" in msg or "format" in msg):
            print("  TIPS: %-tecken i kommentarer/strings måste dubblas till %%")
        return False
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")
        return False


def test_compare_coverage() -> bool:
    """compare_coverage.sql körs som main.py:compare_coverage() — periodliteraler substitueras."""
    body = (SQL_DIR / "compare_coverage.sql").read_text(encoding="utf-8")
    body = body.replace("@period_lo@", "202601").replace("@period_hi@", "202604")
    return _run("compare_coverage.sql", body, ())


def test_coverage_accounts() -> bool:
    """coverage_accounts.sql tar (company_id, period, source_kind)."""
    body = (SQL_DIR / "coverage_accounts.sql").read_text(encoding="utf-8")
    # Bolag 72 / 202604 / SIE — vet att det finns data där (Dala Lås)
    return _run("coverage_accounts.sql", body, (72, "202604", "SIE"))


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


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--file", help="Specifik SQL-fil att testa (ingen wrapping)")
    p.add_argument("--param", "-p", action="append", default=[],
                   help="Parameter för %%s-placeholders (kan upprepas)")
    args = p.parse_args()

    if args.file:
        path = Path(args.file)
        if not path.is_absolute():
            path = REPO / path
        if not path.exists():
            print(f"ERROR: {path} finns inte")
            return 2
        body = path.read_text(encoding="utf-8")
        ok = _run(path.name, body, tuple(args.param))
        return 0 if ok else 1

    # Default: kör alla täcknings-SQL-filer
    results = [
        test_compare_coverage(),
        test_coverage_accounts(),
        test_report_pivot(),
    ]
    if all(results):
        print(f"\nAll {len(results)} tests OK.")
        return 0
    failed = sum(1 for r in results if not r)
    print(f"\n{failed} av {len(results)} tests failade.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
