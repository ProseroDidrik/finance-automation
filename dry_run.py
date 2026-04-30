# -*- coding: utf-8 -*-
"""
Dry-run: match .msg files against Dotterbolagslista.xlsx and show proposed
BolagsID prefix + country subfolder for every mail. Nothing is written to disk.
"""
from __future__ import annotations
import argparse
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path
import extract_msg
import openpyxl

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from shared import (
    load_config as _load_config,
    begin_run as _begin_run,
    load_overrides as _load_overrides,
    country_constraint_from_haystacks as _country_constraint,
)

_BASE         = Path(__file__).resolve().parent
GET_TESTFILES = Path(_load_config()["base_path"])
DOTTERBOLAGSLISTA = _BASE / "_params" / "Dotterbolagslista.xlsx"

KNOWN_COUNTRIES = ("Sweden", "Norway", "Finland", "Denmark", "Germany")

# Overrides läses från _params/overrides.json (delas med extract.py).
OVERRIDES: dict[str, int] = {
    k: int(v) for k, v in _load_overrides()["subject_overrides"].items()
}

INLINE_IMAGE_RE = re.compile(
    r"^(image\d+\.(png|gif|jpg|jpeg|bmp)|img-[0-9a-f\-]{30,})$", re.I
)


def _prev_month_period() -> str:
    today = date.today()
    if today.month == 1:
        return f"{today.year - 1}12"
    return f"{today.year}{today.month - 1:02d}"


def normalize(s):
    s = s.lower()
    s = re.sub(r"[_\W]+", " ", s, flags=re.UNICODE)
    return s.strip()


def tokenize(s):
    return [t for t in normalize(s).split() if len(t) >= 2]


def is_inline(att):
    fn = (att.longFilename or att.shortFilename or att.displayName or "").strip()
    if INLINE_IMAGE_RE.match(fn):
        return True
    cid = getattr(att, "cid", None) or getattr(att, "contentId", None)
    if cid and Path(fn).suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".svg"):
        return True
    return False


def att_name(att, idx):
    for c in (att.longFilename, att.displayName, att.shortFilename):
        if c:
            return c
    return "attachment_{}.bin".format(idx)


def load_companies(path):
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    ws = wb["Data For Company Find"]
    companies = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or row[1] is None:
            continue
        kind = (row[7] or "").strip().lower()
        if kind == "consolidated":
            continue
        bolag_id = int(row[1])
        namn = str(row[0] or "").strip()
        namn_clean = re.sub(r"^\s*\d+\s*", "", namn)
        friendly = str(row[4] or "").strip()
        doman = str(row[9] or "").strip() if len(row) > 9 else ""
        market_raw = str(row[2] or "").strip()
        country = market_raw if market_raw in KNOWN_COUNTRIES else "Other"
        companies.append({
            "id": bolag_id,
            "namn": namn_clean,
            "friendly": friendly,
            "doman": doman,
            "country": country,
            "tokens": list({t for s in (namn_clean, friendly) for t in tokenize(s)}),
        })
    wb.close()
    return companies


WEIGHTS = {"filename": 100, "subject": 80, "att_name": 60, "sender": 40, "body": 20}


def score_company(company, haystacks):
    best = 0
    tokens = company["tokens"]
    if not tokens:
        return 0
    for source, weight in WEIGHTS.items():
        hay = normalize(haystacks.get(source, ""))
        if not hay:
            continue
        if all(t in hay for t in tokens):
            best = max(best, weight)
        else:
            matched = sum(1 for t in tokens if t in hay)
            if matched:
                best = max(best, int(weight * matched / len(tokens) * 0.6))
    if company["doman"] and company["doman"] in haystacks.get("sender", ""):
        best = max(best, WEIGHTS["sender"])
    return best


def match_msg(msg_path, companies, id_index):
    if msg_path.stem in OVERRIDES:
        override_id = OVERRIDES[msg_path.stem]
        company = id_index.get(override_id, {
            "id": override_id, "namn": "ID {}".format(override_id),
            "friendly": "ID {}".format(override_id), "country": "Other",
            "tokens": [], "doman": "",
        })
        return company, 999, "manual"

    try:
        msg = extract_msg.Message(str(msg_path))
    except Exception as e:
        return None, 0, "open error: {}".format(e)

    try:
        att_names = " ".join(att_name(a, i) for i, a in enumerate(msg.attachments) if not is_inline(a))
        haystacks = {
            "filename": msg_path.stem,
            "subject": msg.subject or "",
            "att_name": att_names,
            "sender": msg.sender or "",
            "body": (msg.body or "")[:1500],
        }
        allowed = _country_constraint(haystacks)
        eligible = (
            [c for c in companies if c["country"] in allowed]
            if allowed else companies
        )
        if not eligible:
            return None, 0, "no eligible (country constraint: {})".format("/".join(allowed))
        scores = [(c, score_company(c, haystacks)) for c in eligible]
        scores.sort(key=lambda x: -x[1])
        best_company, best_score = scores[0]
        if best_score == 0:
            return None, 0, "no match (country constraint: {})".format("/".join(allowed) if allowed else "none")
        return best_company, best_score, ""
    finally:
        try:
            msg.close()
        except Exception:
            pass


def fmt(val, width):
    s = str(val)
    return s[:width].ljust(width)


def main():
    parser = argparse.ArgumentParser(description="Dry-run: matcha .msg-filer mot Dotterbolagslista")
    parser.add_argument(
        "--period", "-p", metavar="YYYYMM", default=None,
        help="Period att köra (t.ex. 202603). Standard: föregående månad.",
    )
    args = parser.parse_args()
    period = args.period or _prev_month_period()
    _begin_run("dry_run", period)
    inbox_dir = GET_TESTFILES / "_inbox" / period

    if not inbox_dir.exists():
        sys.exit(
            f"Inbox-mapp saknas: {inbox_dir}\n"
            f"Skapa mappen och lägg .msg-filerna för period {period} där."
        )

    print("Period  : {}".format(period))
    print("Inbox   : {}".format(inbox_dir))
    print("Loading Dotterbolagslista ... ", end="", flush=True)
    companies = load_companies(DOTTERBOLAGSLISTA)
    id_index = {c["id"]: c for c in companies}
    print("{} active companies loaded.\n".format(len(companies)))

    msg_files = sorted(
        p for p in inbox_dir.iterdir()
        if p.is_file() and p.suffix.lower() == ".msg"
    )
    print("Found {} .msg files.\n".format(len(msg_files)))

    W = 130
    print("{:>4}  {:>5}  {:>6}  {}  {}  {}".format(
        "#", "Score", "ID",
        fmt("Country", 11), fmt("Bolag", 33), "Mail"))
    print("-" * W)

    unmatched = []
    low_confidence = []
    manual = []
    matched = 0
    country_counts = Counter()

    results = []
    for msg_path in msg_files:
        company, score, note = match_msg(msg_path, companies, id_index)
        results.append((msg_path, company, score, note))

    for i, (msg_path, company, score, note) in enumerate(results, 1):
        if company is None:
            unmatched.append(msg_path.name)
            print("{:>4}  {:>5}  {:>6}  {}  {}  {}  [NO MATCH]".format(
                i, score, "-", fmt("-", 11), fmt("-", 33), msg_path.name))
        else:
            matched += 1
            country = company.get("country", "Other")
            country_counts[country] += 1
            label = "{:03d} {}".format(company["id"], (company["friendly"] or company["namn"]))
            if score == 999:
                flag = " [MANUAL]"
                manual.append((msg_path.name, company))
            elif score < 40:
                flag = " [LOW]"
                low_confidence.append((msg_path.name, company, score))
            else:
                flag = ""
            score_str = "MAN" if score == 999 else str(score)
            print("{:>4}  {:>5}  {:>6}  {}  {}  {}{}".format(
                i, score_str, company["id"],
                fmt(country, 11), fmt(label, 33), msg_path.name, flag))

    print()
    print("=" * W)
    print("  Matched        : {}/{}".format(matched, len(msg_files)))
    print("  Unmatched      : {}".format(len(unmatched)))
    print("  Manual override: {}".format(len(manual)))
    print("  Low confidence : {}  (score < 40, excluding manuals)".format(len(low_confidence)))
    print()
    print("  Files per country folder:")
    for ctry in list(KNOWN_COUNTRIES) + ["Other"]:
        if country_counts[ctry]:
            print("    extracted/{}/{}/  ->  {} mails".format(period, ctry, country_counts[ctry]))

    if unmatched:
        print()
        print("-- UNMATCHED --")
        for name in unmatched:
            print("  {}".format(name))

    if manual:
        print()
        print("-- MANUAL OVERRIDES --")
        for name, c in manual:
            print("  -> {:03d}  {}  {}  {}".format(
                c["id"], fmt(c.get("country", "Other"), 11),
                fmt(c["friendly"] or c["namn"], 30), name))

    if low_confidence:
        print()
        print("-- LOW CONFIDENCE --")
        for name, c, s in low_confidence:
            print("  score={:>3}  -> {:03d}  {}  {}  {}".format(
                s, c["id"], fmt(c.get("country", "Other"), 11),
                fmt(c["friendly"] or c["namn"], 28), name))

    print()
    print("DRY RUN -- no files written.")


if __name__ == "__main__":
    main()
