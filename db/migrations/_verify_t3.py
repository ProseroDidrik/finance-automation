"""T3 verification — PII-minimering via reporting-vyer.

Kollar att:
  - reporting-schema + tre vyer finns
  - mcp_readonly har SELECT på reporting.* men INTE på PII-råtabellerna
  - etl_writer fortfarande har full DML på PII-råtabellerna
  - vyerna returnerar data (smoke-test mot live data)
  - personnummer-mönster maskas korrekt

Anropas:
    $env:DATABASE_URL_ADMIN = az keyvault secret show --vault-name kv-finauto-6427 `
        --name database-url --query value -o tsv
    .venv\\Scripts\\python.exe db\\migrations\\_verify_t3.py

Exit 0 vid all PASS, 1 annars.
"""
from __future__ import annotations

import os
import re
import sys

import psycopg


PASSED = 0
FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        mark = "PASS"
    else:
        FAILED += 1
        mark = "FAIL"
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{mark}] {label}{suffix}")


PII_TABLES = ("fact_personnel", "fact_journal_sie", "fact_journal_saft")
REPORTING_VIEWS = ("personnel", "journal_sie", "journal_saft")
PNR_RE = re.compile(r"\d{6}[-+]\d{4}")


def main() -> int:
    db_url = os.environ.get("DATABASE_URL_ADMIN")
    if not db_url:
        sys.exit("DATABASE_URL_ADMIN saknas i env")

    with psycopg.connect(db_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            # T3.A — schema reporting finns och mcp_readonly har USAGE
            cur.execute("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='reporting')")
            check("T3.A1 schema reporting finns", cur.fetchone()[0])
            cur.execute(
                "SELECT has_schema_privilege('mcp_readonly','reporting','USAGE')"
            )
            check("T3.A2 mcp_readonly USAGE på reporting", cur.fetchone()[0])

            # T3.B — vyerna finns
            for v in REPORTING_VIEWS:
                cur.execute(
                    "SELECT EXISTS(SELECT 1 FROM information_schema.views "
                    "WHERE table_schema='reporting' AND table_name=%s)",
                    (v,),
                )
                check(f"T3.B   vyn reporting.{v} finns", cur.fetchone()[0])

            # T3.C — mcp_readonly SELECT på reporting-vyerna
            for v in REPORTING_VIEWS:
                cur.execute(
                    "SELECT has_table_privilege('mcp_readonly', %s, 'SELECT')",
                    (f"reporting.{v}",),
                )
                check(f"T3.C   SELECT på reporting.{v}", cur.fetchone()[0])

            # T3.D — mcp_readonly har INGA rättigheter på PII-råtabellerna
            for t in PII_TABLES:
                cur.execute(
                    "SELECT has_table_privilege('mcp_readonly', %s, 'SELECT')",
                    (f"public.{t}",),
                )
                has_select = cur.fetchone()[0]
                check(f"T3.D   mcp_readonly SAKNAR SELECT på public.{t}", not has_select)

            # T3.E — etl_writer behåller full DML på PII-tabellerna
            for t in PII_TABLES:
                cur.execute(
                    "SELECT has_table_privilege('etl_writer', %s, 'SELECT'),"
                    "       has_table_privilege('etl_writer', %s, 'INSERT'),"
                    "       has_table_privilege('etl_writer', %s, 'UPDATE'),"
                    "       has_table_privilege('etl_writer', %s, 'DELETE')",
                    (f"public.{t}",) * 4,
                )
                s, i, u, d = cur.fetchone()
                ok = s and i and u and d
                check(
                    f"T3.E   etl_writer DML på public.{t}",
                    ok,
                    f"S={s},I={i},U={u},D={d}",
                )

            # T3.F — pseudonymisering: reporting.personnel.employee_ref startar med EMP_
            cur.execute("SELECT COUNT(*) FROM reporting.personnel")
            n_personnel = cur.fetchone()[0]
            if n_personnel == 0:
                check("T3.F   reporting.personnel har data", False, "0 rader — kan inte verifiera maskning")
            else:
                cur.execute(
                    "SELECT employee_ref, birth_year FROM reporting.personnel LIMIT 5"
                )
                rows = cur.fetchall()
                all_emp = all(ref.startswith("EMP_") for ref, _ in rows)
                check(
                    f"T3.F1  alla employee_ref startar med 'EMP_' (n={n_personnel} total, kollade {len(rows)})",
                    all_emp,
                )
                # birth_year ska vara INT, inte timestamp
                years_valid = all(
                    by is None or (1900 <= by <= 2030)
                    for _, by in rows
                )
                check("T3.F2  birth_year-värden 1900-2030 eller NULL", years_valid)

            # T3.F3 — reporting.personnel-vyn saknar PII-kolumner
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='reporting' AND table_name='personnel'"
            )
            view_cols = {r[0] for r in cur.fetchall()}
            removed = {"employee_name", "birth_date", "salary_local", "termination_reason"}
            present_removed = view_cols & removed
            check(
                "T3.F3  PII-kolumner borttagna ur reporting.personnel",
                not present_removed,
                f"kvarvarande: {present_removed}" if present_removed else "",
            )

            # T3.G — PNR-maskning fungerar på syntetisk indata.
            # Maskar inte mot riktig data (kan vara tomt, kan vara känsligt
            # att leta) — verifiera regex-beteendet via en testquery.
            cur.execute("""
                SELECT regexp_replace('Anställd 800101-1234 startar', '[0-9]{6}[-+][0-9]{4}', '[PNR]', 'g'),
                       regexp_replace('Test 990229+5678 plus-format',  '[0-9]{6}[-+][0-9]{4}', '[PNR]', 'g'),
                       regexp_replace('Ingen PNR här',                  '[0-9]{6}[-+][0-9]{4}', '[PNR]', 'g')
            """)
            masked1, masked2, masked3 = cur.fetchone()
            check("T3.G1  PNR med - maskas", "[PNR]" in masked1 and "800101" not in masked1, masked1)
            check("T3.G2  PNR med + maskas", "[PNR]" in masked2 and "990229" not in masked2, masked2)
            check("T3.G3  text utan PNR förändras inte", masked3 == "Ingen PNR här", masked3)

            # T3.G4 — applicera samma regex på riktig journal-data (sample) och
            # bekräfta att ingen PNR slipper igenom via reporting-vyn.
            cur.execute("""
                SELECT COUNT(*)
                FROM reporting.journal_sie
                WHERE voucher_text ~ '[0-9]{6}[-+][0-9]{4}'
                   OR transaction_text ~ '[0-9]{6}[-+][0-9]{4}'
            """)
            leaked_sie = cur.fetchone()[0]
            check(
                f"T3.G4  reporting.journal_sie: 0 rader med PNR-mönster kvar",
                leaked_sie == 0,
                f"{leaked_sie} rader läcker" if leaked_sie else "",
            )
            cur.execute("""
                SELECT COUNT(*)
                FROM reporting.journal_saft
                WHERE line_description ~ '[0-9]{6}[-+][0-9]{4}'
                   OR transaction_description ~ '[0-9]{6}[-+][0-9]{4}'
            """)
            leaked_saft = cur.fetchone()[0]
            check(
                f"T3.G5  reporting.journal_saft: 0 rader med PNR-mönster kvar",
                leaked_saft == 0,
                f"{leaked_saft} rader läcker" if leaked_saft else "",
            )

    print(f"\nSummary: {PASSED} pass, {FAILED} fail")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
