"""T2 verification — strukturerad PASS/FAIL-check av etl_writer-rollen.

Anropas:
    $env:DATABASE_URL_ADMIN = az keyvault secret show --vault-name kv-finauto-6427 `
        --name database-url --query value -o tsv
    .venv\\Scripts\\python.exe db\\migrations\\_verify_t2.py

Exit 0 vid all PASS, 1 annars.
"""
from __future__ import annotations

import os
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


def main() -> int:
    db_url = os.environ.get("DATABASE_URL_ADMIN")
    if not db_url:
        sys.exit("DATABASE_URL_ADMIN saknas i env")

    with psycopg.connect(db_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            # T2.A — rollen finns, är vanlig login-roll, inte admin
            cur.execute(
                "SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolconfig "
                "FROM pg_roles WHERE rolname='etl_writer'"
            )
            row = cur.fetchone()
            check("T2.A1 rollen finns", row is not None)
            if row:
                cl, su, by, cdb, crr, cfg = row
                cfg = cfg or []
                check("T2.A2 rolcanlogin=true", cl is True)
                check("T2.A3 rolsuper=false", su is False)
                check("T2.A4 rolbypassrls=false", by is False)
                check("T2.A5 rolcreatedb=false", cdb is False)
                check("T2.A6 rolcreaterole=false", crr is False)
                check(
                    "T2.A7 statement_timeout=600s",
                    "statement_timeout=600s" in cfg,
                    ",".join(cfg),
                )
                # ETL ska INTE ha default_transaction_read_only (vill skriva)
                check(
                    "T2.A8 ingen default_transaction_read_only",
                    not any("default_transaction_read_only" in c for c in cfg),
                    ",".join(cfg),
                )

            # T2.B — INTE medlem i azure_pg_admin / pg_write_all_data
            cur.execute("""
                SELECT EXISTS(
                    SELECT 1 FROM pg_auth_members m
                    JOIN pg_roles r ON r.oid = m.roleid
                    JOIN pg_roles u ON u.oid = m.member
                    WHERE u.rolname='etl_writer'
                      AND r.rolname IN ('azure_pg_admin','pg_write_all_data')
                )
            """)
            check(
                "T2.B   ej medlem i azure_pg_admin/pg_write_all_data",
                not cur.fetchone()[0],
            )

            # T2.C — DML på alla public-tabeller (BASE TABLES bara, inte vyer).
            # pg_tables filtrerar bort extension-vyer (pg_stat_statements m.fl.)
            # som ligger i public-schemat men inte ägs av admin.
            cur.execute(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname='public' ORDER BY 1"
            )
            tables = [r[0] for r in cur.fetchall()]
            check(f"T2.C0  hittade {len(tables)} public-tabeller", len(tables) > 0)
            bad = []
            for t in tables:
                cur.execute(
                    "SELECT has_table_privilege('etl_writer', %s, 'SELECT'),"
                    "       has_table_privilege('etl_writer', %s, 'INSERT'),"
                    "       has_table_privilege('etl_writer', %s, 'UPDATE'),"
                    "       has_table_privilege('etl_writer', %s, 'DELETE'),"
                    "       has_table_privilege('etl_writer', %s, 'TRUNCATE')",
                    (f"public.{t}",) * 5,
                )
                s, i, u, d, tr = cur.fetchone()
                if not (s and i and u and d and tr):
                    bad.append(f"{t}(S={s},I={i},U={u},D={d},T={tr})")
            check(
                "T2.C   alla tabeller: SELECT/INSERT/UPDATE/DELETE/TRUNCATE = t",
                not bad,
                "; ".join(bad) if bad else "",
            )

            # T2.D — sequences: USAGE + SELECT, för DEFAULT nextval()
            cur.execute(
                "SELECT sequence_name FROM information_schema.sequences "
                "WHERE sequence_schema='public' ORDER BY 1"
            )
            seqs = [r[0] for r in cur.fetchall()]
            check(f"T2.D0  hittade {len(seqs)} public-sequences", len(seqs) > 0)
            bad_seq = []
            for s in seqs:
                cur.execute(
                    "SELECT has_sequence_privilege('etl_writer', %s, 'USAGE'),"
                    "       has_sequence_privilege('etl_writer', %s, 'SELECT')",
                    (f"public.{s}",) * 2,
                )
                u, sel = cur.fetchone()
                if not (u and sel):
                    bad_seq.append(f"{s}(U={u},S={sel})")
            check(
                "T2.D   alla sequences: USAGE + SELECT",
                not bad_seq,
                "; ".join(bad_seq) if bad_seq else "",
            )

            # T2.E — INGEN DDL: kan inte CREATE i public, kan inte CREATE i finance
            cur.execute(
                "SELECT has_schema_privilege('etl_writer','public','CREATE'),"
                "       has_schema_privilege('etl_writer','public','USAGE')"
            )
            cc, cu = cur.fetchone()
            check("T2.E1 has_schema_privilege public CREATE=false", not cc)
            check("T2.E2 has_schema_privilege public USAGE=true", cu)
            cur.execute(
                "SELECT has_database_privilege('etl_writer','finance','CONNECT'),"
                "       has_database_privilege('etl_writer','finance','CREATE')"
            )
            cnc, cnd = cur.fetchone()
            check("T2.E3 CONNECT=true", cnc)
            check("T2.E4 CREATE on database=false", not cnd)

    print(f"\nSummary: {PASSED} pass, {FAILED} fail")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
