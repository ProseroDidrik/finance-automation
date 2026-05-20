"""Verifiera SIE_VER-syntesen: ren kumlogik + DB-integration mot bolag 4.

Kör:  py scripts/check_sie_ver.py
Exit 0 = allt OK, 1 = minst ett fel.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db
from load_sie import cumulate_ytd, fy_periods


def check_fy_periods() -> bool:
    ok = fy_periods("202601", "202604") == ["202601", "202602", "202603", "202604"]
    ok = ok and fy_periods("202603", "202603") == ["202603"]
    ok = ok and fy_periods("202511", "202601") == ["202511", "202512", "202601"]
    print(f"[{'OK' if ok else 'FAIL'}]  fy_periods")
    return ok


def check_cumulate() -> bool:
    # 2 konton, 3 månader. Konto 3000 aktivt jan+mar, konto 4000 bara feb.
    periods = ["202601", "202602", "202603"]
    monthly = [
        ("3000", "202601", -100.0),
        ("3000", "202603",  -50.0),
        ("4000", "202602",   30.0),
    ]
    got = {(c, p): round(v, 2) for c, p, v in cumulate_ytd(monthly, periods)}
    want = {
        ("3000", "202601"): -100.0,   # första aktivitet
        ("3000", "202602"): -100.0,   # carry-forward (ingen rörelse feb)
        ("3000", "202603"): -150.0,   # +(-50)
        ("4000", "202602"):   30.0,   # första aktivitet
        ("4000", "202603"):   30.0,   # carry-forward
    }
    ok = got == want
    # Konto med aktivitet helt utanför periods-fönstret ger inga rader.
    oob = cumulate_ytd([("9999", "202512", -200.0)], periods)
    ok = ok and oob == []
    print(f"[{'OK' if ok else 'FAIL'}]  cumulate_ytd")
    if not ok:
        print(f"  want={want}\n  got ={got}\n  oob ={oob}")
    return ok


def check_db_company4() -> bool:
    con = db.connect(read_only=True)
    try:
        ver = con.execute(
            """SELECT amount FROM fact_balances
               WHERE company_id = 4 AND account_code = '3041'
                 AND source_kind = 'SIE_VER' AND period = '202604'"""
        ).fetchone()
        jrnl = con.execute(
            """SELECT SUM(amount) FROM fact_journal_sie
               WHERE company_id = 4 AND account_code = '3041'
                 AND period BETWEEN '202601' AND '202604'"""
        ).fetchone()
        if ver is None:
            print("[FAIL]  bolag 4 3041: ingen SIE_VER-rad för 202604 "
                  "(har load_sie.py körts?)")
            return False
        diff = abs(ver[0] - (jrnl[0] or 0.0))
        ok = diff < 1.0
        print(f"[{'OK' if ok else 'FAIL'}]  bolag 4 3041 SIE_VER YTD apr "
              f"= {ver[0]:.2f}  journal jan..apr = {jrnl[0]:.2f}  diff = {diff:.2f}")
        return ok
    finally:
        con.close()


def check_coverage() -> bool:
    con = db.connect(read_only=True)
    try:
        rows = con.execute(
            """WITH har_psaldo AS (SELECT DISTINCT company_id FROM fact_balances
                                   WHERE source_kind = 'SIE_PSALDO' AND scenario = 'A'),
                    har_sie_ver AS (SELECT DISTINCT company_id FROM fact_balances
                                    WHERE source_kind = 'SIE_VER' AND scenario = 'A'),
                    har_sie AS (SELECT DISTINCT company_id FROM fact_balances
                                WHERE source_kind = 'SIE' AND scenario = 'A')
               SELECT c.company_id, c.name FROM dim_company c
               WHERE c.country = 'Sweden'
                 AND c.company_id IN (SELECT company_id FROM har_sie)
                 AND c.company_id NOT IN (SELECT company_id FROM har_psaldo)
                 AND c.company_id NOT IN (SELECT company_id FROM har_sie_ver)"""
        ).fetchall()
        n = len(rows)
        tag = "OK" if n == 0 else "INFO"
        suffix = "" if n == 0 else " (väntat tills full SE-omladdning körts — rollout steg 1)"
        print(f"[{tag}]  coverage: {n} SE-bolag med SIE men utan PSALDO/SIE_VER{suffix}")
        for cid, nm in rows[:10]:
            print(f"           {cid}  {nm}")
        return n == 0
    finally:
        con.close()


def main() -> None:
    # Deterministiska checkar gatar exit-koden. check_coverage är informativ —
    # den ger 0 först efter full SE-omladdning (rollout steg 1), inte mitt i
    # planen då bara bolag 4 laddats om.
    hard = [check_fy_periods(), check_cumulate(), check_db_company4()]
    check_coverage()
    sys.exit(0 if all(hard) else 1)


if __name__ == "__main__":
    main()
