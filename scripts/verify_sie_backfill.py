"""Manuell integrationsverifiering av SIE analysis-only-backfillen mot lokal
Postgres. RÖR ALDRIG PROD — kräver localhost-DATABASE_URL.

Bevisar: load_sie.backfill_file_analysis fyller fact_sie_analysis MEN lämnar
fact_journal_sie + fact_balances(SIE*) oförändrade, och är idempotent.

    docker start finance-pg-dev
    $env:DATABASE_URL = "postgresql://dev:dev@localhost:5432/finance"
    py scripts/verify_sie_backfill.py
"""
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402

if "localhost" not in os.environ.get("DATABASE_URL", ""):
    sys.exit("VÄGRAR: DATABASE_URL pekar inte på localhost — kör bara mot lokal dev.")


def _counts(con, cid):
    j = con.execute("SELECT COUNT(*) FROM fact_journal_sie WHERE company_id=%s",
                    [cid]).fetchone()[0]
    b = con.execute("SELECT COUNT(*) FROM fact_balances "
                    "WHERE company_id=%s AND source_kind LIKE %s",
                    [cid, "SIE%"]).fetchone()[0]
    a = con.execute("SELECT COUNT(*) FROM fact_sie_analysis WHERE company_id=%s",
                    [cid]).fetchone()[0]
    return j, b, a


def main():
    from shared import load_config
    from load_sie import (backfill_file_analysis, build_orgnr_lookup,
                          read_text_with_fallback, parse_sie, normalize_orgnr,
                          vouchers_to_journal_rows)
    base = Path(load_config()["base_path"])
    ydir = base / "_history" / "2024"
    cand = sorted(p for p in ydir.iterdir()
                  if p.is_file() and p.suffix.upper() in (".SE", ".SI"))
    if not cand:
        sys.exit("Hittade ingen historisk SIE-fil i _history/2024")
    con = db.connect(role="admin")
    try:
        db.init_schema(con)
        lookup = build_orgnr_lookup(con)
        # Välj första SIE-fil vars orgnr finns OCH som faktiskt har dimensioner,
        # annars vore "analys fylld"-kontrollen meningslös.
        chosen = None
        for path in cand:
            parsed = parse_sie(read_text_with_fallback(path), with_journal=True)
            hit = lookup.get(normalize_orgnr(parsed.get("orgnr") or ""))
            if not hit:
                continue
            _j, ar, _p, _s = vouchers_to_journal_rows(
                parsed, hit[0], "SEK", path.name, datetime.now())
            if ar:
                chosen = (path, hit[0], len(ar), bool(parsed.get("psaldo")))
                break
        if not chosen:
            sys.exit("Ingen 2024-SIE-fil med dimensioner + orgnr i dim_company hittades")
        path, cid, n_dims, has_psaldo = chosen
        # Spegla load_year: None om #PSALDO finns, annars årsfallback.
        period_override = None if has_psaldo else "202412"
        print(f"testfil: {path.name}  (company_id={cid}, {n_dims} analysrader, "
              f"period_override={period_override})")

        j0, b0, a0 = _counts(con, cid)
        backfill_file_analysis(con, path, base, period_override, lookup, dry_run=False)
        j1, b1, a1 = _counts(con, cid)
        print(f"[{'OK' if j1==j0 and b1==b0 else 'FAIL'}] journal/balans oforandrade: "
              f"journal {j0}->{j1}, balans {b0}->{b1}")
        print(f"[{'OK' if a1>0 else 'FAIL'}] analys fylld: {a0}->{a1}")

        backfill_file_analysis(con, path, base, period_override, lookup, dry_run=False)
        j2, b2, a2 = _counts(con, cid)
        print(f"[{'OK' if a2==a1 else 'FAIL'}] idempotent: analys {a1}->{a2}")
        print(f"[{'OK' if j2==j0 and b2==b0 else 'FAIL'}] journal/balans fortf. oforandrade")
    finally:
        con.close()


if __name__ == "__main__":
    main()
