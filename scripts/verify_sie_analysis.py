"""Manuell integrationsverifiering: ladda en dim-tung SE-SIE till lokal Postgres
och kontrollera analys-lagret. RÖR ALDRIG PROD — kräver localhost-DATABASE_URL.

    docker start finance-pg-dev
    $env:DATABASE_URL = "postgresql://dev:dev@localhost:5432/finance"
    py scripts/verify_sie_analysis.py
"""
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402
from sie_parser import parse_sie, read_text_with_fallback  # noqa: E402

if "localhost" not in os.environ.get("DATABASE_URL", ""):
    sys.exit("VÄGRAR: DATABASE_URL pekar inte på localhost — kör bara mot lokal dev.")


def period_dist(path):
    """Periodfördelning journal vs analys ur SAMMA parse (samma nycklar förväntas)."""
    parsed = parse_sie(read_text_with_fallback(path), with_journal=True)
    jd, ad = Counter(), Counter()
    for v in parsed["vouchers"]:
        p = v["date"][:6]
        for t in v["transes"]:
            jd[p] += 1
            for _ in t.get("analysis", []):
                ad[p] += 1
    return jd, ad


def main():
    from shared import load_config
    from load_sie import build_orgnr_lookup, load_file, discover_files
    base = Path(load_config()["base_path"])
    files = discover_files(base / "extracted/202604/Sweden")
    if not files:
        sys.exit("Inga SIE-filer i extracted/202604/Sweden")
    sie = files[0]
    con = db.connect(role="admin")
    try:
        db.init_schema(con)
        lookup = build_orgnr_lookup(con)
        load_file(con, sie, base, "202604", lookup, dry_run=False,
                  include_journal=True, override=[], force=False)
        n = con.execute("SELECT COUNT(*) FROM fact_sie_analysis").fetchone()[0]
        types = con.execute(
            "SELECT COUNT(*) FROM dim_analysis_type WHERE source_format='SIE'").fetchone()[0]
        members = con.execute(
            "SELECT COUNT(*) FROM dim_analysis_member WHERE source_format='SIE'").fetchone()[0]
        print(f"[OK] fact_sie_analysis={n} rader, dim_type(SIE)={types}, dim_member(SIE)={members}")
        load_file(con, sie, base, "202604", lookup, dry_run=False,
                  include_journal=True, override=[], force=False)
        n2 = con.execute("SELECT COUNT(*) FROM fact_sie_analysis").fetchone()[0]
        print(f"[{'OK' if n2 == n else 'FAIL'}] idempotens: {n} -> {n2}")
        jd, ad = period_dist(sie)
        ok = set(ad) <= set(jd)
        print(f"[{'OK' if ok else 'FAIL'}] period-bindning (analys subset av journal): "
              f"analys={sorted(ad)} journal={sorted(jd)}")
        row = con.execute(
            """SELECT analysis_type, SUM(amount) FROM fact_sie_analysis
               GROUP BY analysis_type ORDER BY analysis_type""").fetchall()
        print(f"[INFO] sum per analysis_type: {row}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
