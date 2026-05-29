"""No-op-verifiering av FY-fixen på RENA filer (icke-clobber-bolag).

Advisor-fångad blind fläck: floor (jp < fy_start) kan tyst droppa legitima
ingående-balans/årsgräns-linjer (ValueDate YYYY-01-01 eller föreg. YYYY-12-31)
ur välformade filer. För en ren fil ska 0 linjer ligga utanför FY. Rapporterar
below/above-count + exempel-ValueDates på below-linjer (för att se om de är
årsgräns).
"""
import glob, sys
from collections import Counter
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from saft_parser import parse_saft, iter_saft_journal, derive_period, derive_fy_range, _journal_period
from shared import load_config

base = Path(load_config()["base_path"])

# Rena (icke-clobber) bolag enligt guarden: 16, 200, 204. Annuals + ett monthly.
patterns = [
    "_history/2023/Troms* 247*",      # 16 annual 2023
    "_history/2024/Troms* 247*",      # 16 annual 2024
    "_history/2023/Lofoten PO*",      # 200 annual 2023
    "_history/2024/Lofoten PO*",      # 200 annual 2024
    "extracted/202604/Norway/016_*",  # 16 monthly 202604
    "extracted/202604/Norway/204_*",  # 204 monthly 202604
]

for pat in patterns:
    hits = glob.glob(str(base / pat))
    if not hits:
        print(f"\n[SAKNAS] {pat}")
        continue
    path = Path(hits[0])
    parsed = parse_saft(path)
    period = derive_period(parsed, None)
    fy_start, fy_end = derive_fy_range(parsed, period)
    below = above = total = 0
    below_examples = Counter()
    for j in iter_saft_journal(path, parsed["ns"]):
        total += 1
        jp = _journal_period(j, period)
        if jp < fy_start:
            below += 1
            d = j.get("value_date") or j.get("transaction_date")
            below_examples[str(d)[:10]] += 1
        elif jp > fy_end:
            above += 1
    flag = "OK (no-op)" if below == 0 and above == 0 else "!!! DROPPAR LINJER"
    print(f"\n{path.name[:55]}")
    print(f"  FY={fy_start}-{fy_end} total_linjer={total} below_FY={below} above_FY={above}  {flag}")
    if below_examples:
        print(f"  below-exempel (ValueDate→antal): {dict(below_examples.most_common(8))}")
