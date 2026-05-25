"""T1 verification — strukturerad PASS/FAIL-check av mcp_readonly-rollen.

Återskapar 13 acceptanskriterier från 20260525_mcp_readonly_role.verify.sql,
men som Python så vi får tydlig pass/fail-output utan psql-installation.

Anropas typiskt:
    $env:DATABASE_URL_ADMIN = az keyvault secret show --vault-name kv-finauto-6427 `
        --name database-url --query value -o tsv
    .venv\\Scripts\\python.exe db\\migrations\\_verify_t1.py

Exit-code: 0 om alla PASS, 1 annars.
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
            # T1.A — rollen finns och saknar admin-flaggor
            cur.execute(
                "SELECT rolcanlogin, rolsuper, rolbypassrls, rolconfig "
                "FROM pg_roles WHERE rolname='mcp_readonly'"
            )
            row = cur.fetchone()
            check("T1.A1 rollen finns", row is not None)
            if row:
                cl, su, by, cfg = row
                cfg = cfg or []
                check("T1.A2 rolcanlogin=true", cl is True)
                check("T1.A3 rolsuper=false", su is False)
                check("T1.A4 rolbypassrls=false", by is False)
                check(
                    "T1.A5 default_transaction_read_only=on",
                    "default_transaction_read_only=on" in cfg,
                    ",".join(cfg),
                )
                check(
                    "T1.A6 statement_timeout=30s",
                    "statement_timeout=30s" in cfg,
                    ",".join(cfg),
                )

            # T1.B — INTE medlem i azure_pg_admin / pg_write_all_data
            cur.execute("""
                SELECT EXISTS(
                    SELECT 1 FROM pg_auth_members m
                    JOIN pg_roles r ON r.oid = m.roleid
                    JOIN pg_roles u ON u.oid = m.member
                    WHERE u.rolname='mcp_readonly'
                      AND r.rolname IN ('azure_pg_admin','pg_write_all_data')
                )
            """)
            check(
                "T1.B   ej medlem i azure_pg_admin/pg_write_all_data",
                not cur.fetchone()[0],
            )

            # T1.C — SELECT på alla public-tabeller (BASE TABLES bara).
            # pg_tables filtrerar bort extension-vyer (pg_stat_statements m.fl.)
            # som ligger i public men inte ägs av admin.
            cur.execute(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname='public' ORDER BY 1"
            )
            tables = [r[0] for r in cur.fetchall()]
            check(f"T1.C0  hittade {len(tables)} public-tabeller", len(tables) > 0)
            bad = []
            for t in tables:
                cur.execute(
                    "SELECT has_table_privilege('mcp_readonly', %s, 'SELECT'),"
                    "       has_table_privilege('mcp_readonly', %s, 'INSERT'),"
                    "       has_table_privilege('mcp_readonly', %s, 'UPDATE'),"
                    "       has_table_privilege('mcp_readonly', %s, 'DELETE')",
                    (f"public.{t}",) * 4,
                )
                s, i, u, d = cur.fetchone()
                if not (s and not i and not u and not d):
                    bad.append(f"{t}(S={s},I={i},U={u},D={d})")
            check(
                "T1.C   alla tabeller: SELECT=t, INSERT/UPDATE/DELETE=f",
                not bad,
                "; ".join(bad) if bad else "",
            )

            # T1.D — public-schema-rättigheter
            cur.execute(
                "SELECT has_schema_privilege('mcp_readonly','public','CREATE'),"
                "       has_schema_privilege('mcp_readonly','public','USAGE')"
            )
            cc, cu = cur.fetchone()
            check("T1.D1 has_schema_privilege public CREATE=false", not cc)
            check("T1.D2 has_schema_privilege public USAGE=true", cu)

            # T1.E — DB-rättigheter
            cur.execute(
                "SELECT has_database_privilege('mcp_readonly','finance','CONNECT'),"
                "       has_database_privilege('mcp_readonly','finance','CREATE')"
            )
            cnc, cnd = cur.fetchone()
            check("T1.E1 CONNECT=true", cnc)
            check("T1.E2 CREATE=false", not cnd)

    print(f"\nSummary: {PASSED} pass, {FAILED} fail")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
