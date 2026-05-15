import sys, openpyxl
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")

# Snabb sum-check på existerande INL-filer för 145/153/195 över 202601-202603
ROOT = Path(r"C:\Users\DidWac\Prosero Dropbox\Didrik Wachtmeister\Phoenix Foundation\April alla filer\Get testfiles\extracted")
for period in ("202601", "202602", "202603", "202604"):
    base = ROOT / period / "Finland" / "output"
    print(f"\n=== {period} ===")
    for code, friendly in [(145, "Prosero Security Oy"), (153, "Turvatalo"), (195, "Meri-Lapin")]:
        # Försök hitta INL-filen (filnamn kan variera ngt)
        candidates = list(base.glob(f"{code}_*_{period}_INL.xlsx"))
        if not candidates:
            print(f"  {code}: (ingen INL hittad)")
            continue
        f = candidates[0]
        wb = openpyxl.load_workbook(str(f), data_only=True)
        ws = wb.active
        total = 0.0
        n = 0
        for r in range(2, ws.max_row + 1):
            v = ws.cell(row=r, column=3).value
            if isinstance(v, (int, float)):
                total += v
                n += 1
        wb.close()
        print(f"  {code}: rows={n:3d}, sum={total:>12,.2f}  ({f.name})")
