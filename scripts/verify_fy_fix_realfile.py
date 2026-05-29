"""Real-file-verifiering av FY-range-fixen mot bolag 9:s 202604-fil (den som
clobbrade 202203 via 2022-daterade strö-rader). Ingen DB — bara parse + gruppering.
Visar perioder FÖRE och EFTER FY-floor.
"""
import glob, os, sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import load_saft
from saft_parser import parse_saft, iter_saft_journal, derive_period, derive_fy_range
from shared import load_config

base = load_config()["base_path"]
fp = glob.glob(os.path.join(base, "extracted", "202604", "Norway", "009*"))[0]
path = Path(fp)
parsed = parse_saft(path)
period = derive_period(parsed, None)
fy_start, fy_end = derive_fy_range(parsed, period)
print(f"fil-period={period}  FY-range={fy_start}-{fy_end}")

now = datetime.now()
lines = list(iter_saft_journal(path, parsed["ns"]))

before = load_saft.group_analysis_by_period(lines, 9, "NOK", "x", now, period)
after = load_saft.group_analysis_by_period(lines, 9, "NOK", "x", now, period,
                                           period_floor=fy_start,
                                           period_cutoff=fy_end)
def out_of_fy(d):
    return sorted(p for p in d if p < fy_start or p > fy_end)

print(f"\nFÖRE fix: {len(before)} perioder, out-of-FY={out_of_fy(before)}")
print(f"EFTER fix: {len(after)} perioder, out-of-FY={out_of_fy(after)}")
print(f"droppade perioder: {sorted(set(before) - set(after))}")
print(f"behållna in-FY-perioder: {sorted(after)}")
