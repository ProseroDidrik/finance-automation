"""Jämför 202603 vs 202604 INL för 145/153/195 — vilka konton diff:ar?"""
import sys, openpyxl
from pathlib import Path
from collections import defaultdict
sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(r"C:\Users\DidWac\Prosero Dropbox\Didrik Wachtmeister\Phoenix Foundation\April alla filer\Get testfiles\extracted")

def read_inl(p):
    wb = openpyxl.load_workbook(str(p), data_only=True)
    ws = wb.active
    rows = {}
    for r in range(2, ws.max_row + 1):
        acc = ws.cell(row=r, column=1).value
        name = ws.cell(row=r, column=2).value
        v = ws.cell(row=r, column=3).value
        if not isinstance(v, (int, float)):
            continue
        rows[acc] = (str(name)[:40], v)
    wb.close()
    return rows

for code, friendly in [(145, "Prosero Security Oy"), (153, "Turvatalo"), (195, "Meri-Lapin")]:
    print(f"\n========== {code} {friendly} ==========")
    p_mar = list((ROOT / "202603" / "Finland" / "output").glob(f"{code}_*_202603_INL.xlsx"))[0]
    p_apr = list((ROOT / "202604" / "Finland" / "output").glob(f"{code}_*_202604_INL.xlsx"))[0]
    mar = read_inl(p_mar)
    apr = read_inl(p_apr)
    in_apr_not_mar = sorted(set(apr) - set(mar))
    in_mar_not_apr = sorted(set(mar) - set(apr))
    print(f"  Konton i 202604 men inte 202603: {len(in_apr_not_mar)}")
    sum_new = 0.0
    for a in in_apr_not_mar:
        name, v = apr[a]
        sum_new += v
        print(f"    +{a:>5}  {name:<42} {v:>12,.2f}")
    print(f"    Sum nya konton:                                  {sum_new:>12,.2f}")
    print(f"  Konton i 202603 men inte 202604: {len(in_mar_not_apr)}")
    sum_gone = 0.0
    for a in in_mar_not_apr:
        name, v = mar[a]
        sum_gone += v
        print(f"    -{a:>5}  {name:<42} {v:>12,.2f}")
    print(f"    Sum borttagna:                                   {sum_gone:>12,.2f}")
