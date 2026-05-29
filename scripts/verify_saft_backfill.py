"""Manuell integrationsverifiering av historik-backfillen mot lokal Postgres.
RÖR ALDRIG PROD — kräver localhost-DATABASE_URL.

Bevisar: backfill_file_analysis fyller fact_saft_analysis MEN lämnar
fact_journal_saft + fact_balances oförändrade, och är idempotent.

    docker start finance-pg-dev
    $env:DATABASE_URL = "postgresql://dev:dev@localhost:5432/finance"
    py scripts/verify_saft_backfill.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402

if "localhost" not in os.environ.get("DATABASE_URL", ""):
    sys.exit("VÄGRAR: DATABASE_URL pekar inte på localhost — kör bara mot lokal dev.")


def _counts(con, cid):
    j = con.execute("SELECT COUNT(*) FROM fact_journal_saft WHERE company_id=%s",
                    [cid]).fetchone()[0]
    b = con.execute("SELECT COUNT(*) FROM fact_balances "
                    "WHERE company_id=%s AND source_kind='SAFT'", [cid]).fetchone()[0]
    a = con.execute("SELECT COUNT(*) FROM fact_saft_analysis WHERE company_id=%s",
                    [cid]).fetchone()[0]
    return j, b, a


def main():
    from shared import load_config
    from load_saft import backfill_file_analysis, build_orgnr_lookup
    from load_saft import parse_saft, normalize_orgnr
    base = Path(load_config()["base_path"])
    cand = sorted(p for p in (base / "_history" / "2024").glob("*.xml")
                  if "Actas" not in p.name)
    if not cand:
        sys.exit("Hittade ingen historisk NO-xml i _history/2024")
    path = cand[0]
    con = db.connect(role="admin")
    try:
        db.init_schema(con)
        lookup = build_orgnr_lookup(con)
        parsed = parse_saft(path)
        hit = lookup.get(normalize_orgnr(parsed["orgnr"] or ""))
        if not hit:
            sys.exit(f"orgnr {parsed.get('orgnr')} saknas i lokal dim_company — välj annan fil")
        cid = hit[0]
        print(f"testfil: {path.name}  (company_id={cid})")
        j0, b0, a0 = _counts(con, cid)
        backfill_file_analysis(con, path, base, "202412", lookup, dry_run=False)
        j1, b1, a1 = _counts(con, cid)
        print(f"[{'OK' if j1==j0 and b1==b0 else 'FAIL'}] journal/balans orörda: "
              f"journal {j0}->{j1}, balans {b0}->{b1}")
        print(f"[{'OK' if a1>0 else 'FAIL'}] analys fylld: {a0}->{a1}")
        backfill_file_analysis(con, path, base, "202412", lookup, dry_run=False)
        j2, b2, a2 = _counts(con, cid)
        print(f"[{'OK' if a2==a1 else 'FAIL'}] idempotent: analys {a1}->{a2}")
        print(f"[{'OK' if j2==j0 and b2==b0 else 'FAIL'}] journal/balans fortf. orörda")
    finally:
        con.close()


if __name__ == "__main__":
    main()
