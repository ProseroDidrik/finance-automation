"""Verifiera att fact_journal_saft inte dubbelräknar verifikat.

Kör:  py scripts/check_saft_journal_dups.py
Exit 0 = inga dubbletter, 1 = minst en (bolag, period) får journal från
flera källfiler.

Regressionstest för buggen där load_saft.py deduppade fact_journal_saft per
source_file istället för per period — överlappande månads-SAF-T (YTD) lade då
samma månad en gång per efterföljande fil. En korrekt laddad (bolag, period)
ska bara ha journal från EN källfil (den senaste som täcker perioden).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db


def main() -> None:
    con = db.connect(read_only=True)
    try:
        rows = con.execute(
            """SELECT j.company_id, j.period, COUNT(DISTINCT j.source_file) AS n_src
               FROM fact_journal_saft j
               GROUP BY j.company_id, j.period
               HAVING COUNT(DISTINCT j.source_file) > 1
               ORDER BY n_src DESC, j.company_id, j.period"""
        ).fetchall()
    finally:
        con.close()

    if not rows:
        print("[OK]  fact_journal_saft: inga (bolag, period) med journal "
              "från flera källfiler")
        sys.exit(0)

    print(f"[FAIL]  {len(rows)} (bolag, period)-par får journal från flera "
          f"källfiler (dubbelräkning):")
    for cid, per, n in rows[:25]:
        print(f"          bolag {cid}  period {per}  n_source_files={n}")
    sys.exit(1)


if __name__ == "__main__":
    main()
