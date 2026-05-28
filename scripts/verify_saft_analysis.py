"""Manuell integrationsverifiering: ladda NO 009 till lokal Postgres och
kontrollera analys-lagret. RÖR ALDRIG PROD — kräver localhost-DATABASE_URL.

    docker start finance-pg-dev
    $env:DATABASE_URL = "postgresql://dev:dev@localhost:5432/finance"
    py scripts/verify_saft_analysis.py
"""
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402
from saft_parser import iter_saft_journal, parse_saft, _journal_period  # noqa: E402

if "localhost" not in os.environ.get("DATABASE_URL", ""):
    sys.exit("VÄGRAR: DATABASE_URL pekar inte på localhost — kör bara mot lokal dev.")


def period_dist(path, fallback):
    """Periodfördelning journal vs analys ur SAMMA iter (ska ha samma nycklar)."""
    jd, ad = Counter(), Counter()
    ns = parse_saft(path)["ns"]
    for j in iter_saft_journal(path, ns):
        jp = _journal_period(j, fallback)
        jd[jp] += 1
        for _ in j["analysis"]:
            ad[jp] += 1
    return jd, ad


def main():
    from shared import load_config
    from load_saft import build_orgnr_lookup, load_file
    base = Path(load_config()["base_path"])
    no = next((base / "extracted/202604/Norway").glob("009_*.xml"))
    con = db.connect(role="admin")
    try:
        db.init_schema(con)
        lookup = build_orgnr_lookup(con)
        # Global override → tvinga (om)laddning oavsett befintlig SAFT.
        load_file(con, no, base, "202604", lookup, dry_run=False,
                  include_journal=True, override=[], force=False)
        n = con.execute("SELECT COUNT(*) FROM fact_saft_analysis").fetchone()[0]
        types = con.execute("SELECT COUNT(*) FROM dim_analysis_type").fetchone()[0]
        members = con.execute("SELECT COUNT(*) FROM dim_analysis_member").fetchone()[0]
        print(f"[OK] fact_saft_analysis={n} rader, dim_type={types}, dim_member={members}")
        # Idempotens: ladda om → radantal oförändrat (inga dubbletter).
        load_file(con, no, base, "202604", lookup, dry_run=False,
                  include_journal=True, override=[], force=False)
        n2 = con.execute("SELECT COUNT(*) FROM fact_saft_analysis").fetchone()[0]
        print(f"[{'OK' if n2 == n else 'FAIL'}] idempotens: {n} -> {n2}")
        # Period-bindning: journal- och analys-fördelning ska ha samma periodnycklar.
        jd, ad = period_dist(no, "202604")
        ok = set(jd) == set(ad)
        print(f"[{'OK' if ok else 'FAIL'}] period-bindning (samma nycklar journal/analys): {sorted(jd)}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
