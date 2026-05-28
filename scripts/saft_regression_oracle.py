"""Regressions-orakel för SAF-T-parsning (Etapp 4 säkerhetsnät).

Fångar nuvarande `parse_saft` (header + accounts) och `iter_saft_journal`
(verifikatrader) över riktiga NO+DK-filer som deterministiska SHA-256-fingrar.
Refaktorn (NO → xsdata) måste reproducera EXAKT samma fingerprint — annars har
parsningskontraktet ändrats.

Endast hashar + strukturella antal lagras i golden — inga orgnr, bolagsnamn
eller belopp i klartext, så `tests/saft_oracle_golden.json` är ofarlig att
committa (riktiga SAF-T-filer ligger utanför repo:t, under base_path).

Filer nycklas på `{country}_{prefix}` (prefix = ledande siffror i filnamnet =
BolagsID), inte filnamn, så inga bolagsidentifierande strängar committas.

Kontraktet som låses är exakt de fält `load_saft.load_file` konsumerar:
  accounts: (code, name, amount, statement_type, row_index)
  journal : journal_id/desc, transaction_id/date/desc, value_date, line_no,
            record_id, account_code, line_desc, debit, credit

Bruk:
  py scripts/saft_regression_oracle.py --capture          # skriv golden (202604)
  py scripts/saft_regression_oracle.py --capture --period 202604
  py scripts/saft_regression_oracle.py --verify           # jämför mot golden
  py scripts/saft_regression_oracle.py --verify --slow    # inkl. tunga DK 081

DK Actas (081, 221 MB) hoppas över utan --slow — DK-parsningen ändras inte av
refaktorn (NO går till xsdata, DK stannar på iterparse), så snabb-loopen täcker
NO-filerna + DK 054 där det faktiskt händer något.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

# Importera ur projektroten (scripts/ ligger en nivå ner)
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import load_saft  # noqa: E402
from shared import load_config  # noqa: E402

GOLDEN_PATH = Path(__file__).resolve().parent.parent / "tests" / "saft_oracle_golden.json"

# DK Actas — 221 MB, hoppas över utan --slow
HEAVY_PREFIXES = {"DK_081"}

COUNTRY_DIRS = {"Norway": "NO", "Denmark": "DK"}


def _h(*parts: object) -> str:
    """Stabil sträng-join för hashning ('|'-separerad, None → 'None')."""
    return "|".join("None" if p is None else str(p) for p in parts)


def _date_str(d) -> str:
    """datetime.date/None → ISO-sträng eller 'None'."""
    return d.isoformat() if d is not None else "None"


def fingerprint(path: Path) -> dict:
    """Deterministiskt fingerprint av nuvarande parse-output för en fil."""
    parsed = load_saft.parse_saft(path)

    # Header — hasha de identifierande fälten (orgnr/namn/period) i klartext-fritt format.
    header_sha = hashlib.sha256(_h(
        parsed.get("orgnr"), parsed.get("name"),
        parsed.get("period_start_year"), parsed.get("period_start_month"),
        parsed.get("period_end_year"), parsed.get("period_end_month"),
        parsed.get("selection_start_date"), parsed.get("selection_end_date"),
    ).encode("utf-8")).hexdigest()

    # Accounts — (code, name, amount, statement_type, row_index)
    acc = hashlib.sha256()
    rows = parsed["accounts"]
    for code, name, amt, st, idx in rows:
        acc.update((_h(code, name, repr(amt), st, idx) + "\n").encode("utf-8"))

    # Journal — exakt de fält load_file läser, i yield-ordning.
    # Analys (dimensioner) fingerprintas separat: per linje hashas (txn,line,
    # type,id) i yield-ordning så att analys-utfallet regressionsskyddas utan
    # att blandas in i journal_sha256 (det förblir de 12 ursprungsfälten).
    jour = hashlib.sha256()
    ana = hashlib.sha256()
    n_journal = 0
    n_analysis = 0
    for j in load_saft.iter_saft_journal(path, parsed["ns"]):
        n_journal += 1
        jour.update((_h(
            j["journal_id"], j["journal_desc"],
            j["transaction_id"], _date_str(j["transaction_date"]), j["transaction_desc"],
            _date_str(j["value_date"]),
            j["line_no"], j["record_id"], j["account_code"], j["line_desc"],
            repr(j["debit"]), repr(j["credit"]),
        ) + "\n").encode("utf-8"))
        for atype, aid in j["analysis"]:
            ana.update((_h(j["transaction_id"], j["line_no"], atype, aid) + "\n").encode("utf-8"))
            n_analysis += 1

    return {
        "country": parsed.get("country"),
        "currency": parsed.get("currency"),
        "n_accounts": len(rows),
        "header_sha256": header_sha,
        "accounts_sha256": acc.hexdigest(),
        "n_journal": n_journal,
        "journal_sha256": jour.hexdigest(),
        "n_analysis": n_analysis,
        "analysis_sha256": ana.hexdigest(),
    }


def discover(period: str) -> dict[str, Path]:
    """{key: path} för alla riktiga SAF-T-filer under extracted/{period}/."""
    cfg = load_config()
    base = Path(cfg["base_path"])
    out: dict[str, Path] = {}
    for sub, cc in COUNTRY_DIRS.items():
        d = base / "extracted" / period / sub
        if not d.exists():
            continue
        for f in sorted(d.glob("*.xml")):
            m = re.match(r"^(\d+)_", f.name)
            prefix = m.group(1) if m else f.stem
            key = f"{cc}_{prefix}"
            # Kollisionsskydd (osannolikt — en fil per bolag/land/period)
            if key in out:
                key = f"{key}_{len(out)}"
            out[key] = f
    return out


def capture(period: str) -> dict:
    files = discover(period)
    golden = {"_period": period, "files": {}}
    for key, path in files.items():
        golden["files"][key] = fingerprint(path)
        print(f"[CAPTURE] {key}: {golden['files'][key]['n_accounts']} konton, "
              f"{golden['files'][key]['n_journal']} journalrader")
    GOLDEN_PATH.write_text(json.dumps(golden, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Skrev {GOLDEN_PATH} ({len(golden['files'])} filer)")
    return golden


def verify(period: str | None = None, *, slow: bool = False) -> list[str]:
    """Jämför nuvarande parse-output mot golden. Returnerar mismatch-rader."""
    if not GOLDEN_PATH.exists():
        return [f"golden saknas: {GOLDEN_PATH} — kör --capture först"]
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    period = period or golden.get("_period")
    files = discover(period)
    mismatches: list[str] = []

    expected_keys = set(golden["files"])
    actual_keys = set(files)
    if not slow:
        expected_keys -= HEAVY_PREFIXES
        actual_keys -= HEAVY_PREFIXES

    for key in sorted(expected_keys - actual_keys):
        mismatches.append(f"{key}: i golden men filen saknas nu")
    for key in sorted(actual_keys - expected_keys):
        mismatches.append(f"{key}: ny fil utan golden-post")

    for key in sorted(expected_keys & actual_keys):
        got = fingerprint(files[key])
        exp = golden["files"][key]
        for field in ("country", "currency", "n_accounts", "header_sha256",
                      "accounts_sha256", "n_journal", "journal_sha256",
                      "n_analysis", "analysis_sha256"):
            if got[field] != exp[field]:
                mismatches.append(f"{key}.{field}: golden={exp[field]} != nu={got[field]}")
    return mismatches


def main() -> None:
    ap = argparse.ArgumentParser(description="SAF-T regressions-orakel")
    ap.add_argument("--period", default="202604")
    ap.add_argument("--capture", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--slow", action="store_true", help="inkl. tunga DK 081 Actas")
    args = ap.parse_args()

    if args.capture:
        capture(args.period)
    elif args.verify:
        ms = verify(args.period, slow=args.slow)
        if ms:
            print(f"MISMATCH ({len(ms)}):")
            for m in ms:
                print(f"  {m}")
            sys.exit(1)
        print("OK — fingerprint matchar golden")
    else:
        ap.error("ange --capture eller --verify")


if __name__ == "__main__":
    main()
