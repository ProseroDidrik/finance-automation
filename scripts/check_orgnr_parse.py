"""Verifiera #ORGNR-parsning i load_sie.parse_sie — citerad/spaced norsk form.

Kör:  py scripts/check_orgnr_parse.py
Exit 0 = OK, 1 = minst ett fel.

Regressionstest för buggen där `#ORGNR "NO 971199954 MVA"` (norsk Global-
export) truncerades till 'NO'/'989' eftersom regexen bara fångade första
whitespace-fria token. Norska orgnr skrivs ofta citerade med mellanslag
("989 285 246") och prefix/suffix ("NO ... MVA").
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from load_sie import normalize_orgnr, parse_sie

CASES = [
    # (#ORGNR-rad, förväntat normaliserat orgnr)
    ('#ORGNR "NO 971199954 MVA"', "971199954"),   # Buysec — citerat, prefix+suffix
    ('#ORGNR "989 285 246 MVA"',  "989285246"),   # Buytec — citerat, mellanslag
    ('#ORGNR 5560712340',         "5560712340"),  # svensk ociterad (regression)
    ('#ORGNR 556071-2340 1',      "5560712340"),  # svensk, bindestreck + förvnr
    ('#ORGNR "5560712340"',       "5560712340"),  # citerad utan mellanslag
]


def main() -> None:
    ok = True
    for line, expected in CASES:
        parsed = parse_sie(line + "\n")
        got = normalize_orgnr(parsed.get("orgnr") or "")
        passed = got == expected
        ok = ok and passed
        print(f"[{'OK' if passed else 'FAIL'}]  {line!r}  ->  {got!r}"
              f"{'' if passed else f'  (vantat {expected!r})'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
