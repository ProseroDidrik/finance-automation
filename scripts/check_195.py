import sys, re
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")

ACC_RE = re.compile(r"^(\d+)[\s,]+(.+)$")
def parse_acc(text):
    m = ACC_RE.match(str(text).strip())
    return (int(m.group(1)), m.group(2).strip()) if m else None

def parse_amt(v):
    s = str(v).strip().replace(" ", "").replace(",", ".")
    try: return float(s)
    except: return 0.0

def n4(a):
    s = str(a)
    return int(s[:4]) if len(s) > 4 else a

def should_flip(a): return 1 <= n4(a) <= 1999
def is_237x(a): return 2370 <= n4(a) <= 2379
def is_22xx(a): return 2200 <= n4(a) <= 2299

BASE = Path(r"C:\Users\DidWac\Prosero Dropbox\Didrik Wachtmeister\Phoenix Foundation\April alla filer\Get testfiles\_uploads\202604")
BS = BASE / "195_Tase_Meri-Lapin_Lukituspalvelu_Oy_(01.04.2026-30.04.2026).csv"
IS_ = BASE / "195_Tuloslaskelma_Meri-Lapin_Lukituspalvelu_Oy_(01.04.2026-30.04.2026).csv"

# BS: Tase col 2 = Muutos
bs_total = 0; bs_n = 0
bs_rows = []
with open(BS, encoding="utf-16-le") as f:
    for line in f:
        cells = [c.strip('"') for c in line.strip().split(";")]
        if len(cells) < 3: continue
        p = parse_acc(cells[0])
        if not p: continue
        acc, name = p
        amt = parse_amt(cells[2])
        if amt == 0: continue
        if is_237x(acc) or is_22xx(acc): continue
        if should_flip(acc): amt = -amt
        bs_total += amt
        bs_n += 1
        bs_rows.append((acc, name, amt))

# IS: col 1 monthly, col 2 YTD
for col, label in [(1, "monthly"), (2, "YTD")]:
    is_total = 0; is_n = 0
    with open(IS_, encoding="utf-16-le") as f:
        for line in f:
            cells = [c.strip('"') for c in line.strip().split(";")]
            if len(cells) < 3: continue
            p = parse_acc(cells[0])
            if not p: continue
            acc, name = p
            amt = parse_amt(cells[col])
            if amt == 0: continue
            if is_237x(acc) or is_22xx(acc): continue
            is_total += amt
            is_n += 1
    print(f"IS-{label}: n={is_n}, sum={is_total:>12,.2f}; BS: n={bs_n}, sum={bs_total:>12,.2f}; Total: {is_total + bs_total:>10,.2f}")
