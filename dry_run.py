# -*- coding: utf-8 -*-
"""
Dry-run: match .msg files against Dotterbolagslista.xlsx and show proposed
BolagsID prefix + country subfolder for every mail. Nothing is written to disk.
"""
from __future__ import annotations
import re
from pathlib import Path
from collections import Counter
import extract_msg
import openpyxl

_BASE = Path(__file__).resolve().parent

def _resolve_get_testfiles():
    candidates = [
        Path(r"C:\Users\DidWac\Prosero Dropbox\Didrik Wachtmeister\Phoenix Foundation\April alla filer\Get testfiles"),
        Path("/sessions/nifty-loving-albattani/mnt/Get testfiles"),
    ]
    for c in candidates:
        if c.exists():
            return c
    # last-resort fallback so it still imports
    return candidates[0]

GET_TESTFILES = _resolve_get_testfiles()
GET_TESTFILES_DIR = GET_TESTFILES / "_inbox"
DOTTERBOLAGSLISTA = _BASE / "_params" / "Dotterbolagslista.xlsx"

KNOWN_COUNTRIES = ("Sweden", "Norway", "Finland", "Denmark", "Germany")

OVERRIDES = {
    "Mars Säkerhetspartner i väst ": 239,
    "VB_ GF Sich_ GmbH - Auswertungen 3_2026": 245,
    "VB_ Monthly Financial Report H+W mechatronik GmbH (4)": 246,
    "VB_ Monthly Financial Report H+W mechatronik GmbH": 246,
    "Månad mars _)": 33,
    "Q1 2026 rapportering i excel": 111,
    "Untitled": 164,
    "ST Hälytys reports": 196,
    # April 2026 — manuella korrigeringar
    "Doorway mars": 162,
    "SIE Montageservice": 94,
    "SIE-fil Dala Lås i Ludvika AB 2603": 73,
    "Låskomfort mars": 88,
    "Månadsavstämning 2026-03-31 (2)": 93,
    # OBS: "Nylunds och Norrskydd mars" hanteras via ATTACHMENT_OVERRIDES i extract.py
    # (två bolag i samma mail — bilaganamn avgör vilket prefix)
}

INLINE_IMAGE_RE = re.compile(
    r"^(image\d+\.(png|gif|jpg|jpeg|bmp)|img-[0-9a-f\-]{30,})$", re.I
)


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
        avsandare = str(row[8] or "").strip() if len(row) > 8 else ""
        doman = str(row[9] or "").strip() if len(row) > 9 else ""
        market_raw = str(row[2] or "").strip()
        country = market_raw if market_raw in KNOWN_COUNTRIES else "Other"
        companies.append({
            "id": bolag_id,
            "namn": namn_clean,
            "friendly": friendly,
            "avsandare": avsandare,
            "doman": doman,
            "country": country,
            "tokens": list({t for s in (namn_clean, friendly) for t in tokenize(s)}),
        })
    wb.close()
    return companies


WEIGHTS = {
    "filename": 100,
    "subject": 80,
    "att_name": 60,
    "sender": 40,
    "body": 20,
}


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
            if weight > best:
                best = weight
        else:
            matched = sum(1 for t in tokens if t in hay)
            if matched > 0:
                partial = int(weight * matched / len(tokens) * 0.6)
                if partial > best:
                    best = partial
    doman = company["doman"]
    if doman and doman in haystacks.get("sender", ""):
        best = max(best, WEIGHTS["sender"])
    return best


def match_msg(msg_path, companies, id_index):
    if msg_path.stem in OVERRIDES:
        override_id = OVERRIDES[msg_path.stem]
        company = id_index.get(override_id, {
            "id": override_id,
            "namn": "ID {}".format(override_id),
            "friendly": "ID {}".format(override_id),
            "country": "Other",
            "tokens": [],
        })
        return company, 999, "manual"

    try:
        msg = extract_msg.Message(str(msg_path))
    except Exception as e:
        return None, 0, "open error: {}".format(e)

    try:
        subject = msg.subject or ""
        sender = msg.sender or ""
        body = (msg.body or "")[:1500]
        att_names = " ".join(
            att_name(a, i) for i, a in enumerate(msg.attachments) if not is_inline(a)
        )
        haystacks = {
            "filename": msg_path.stem,
            "subject": subject,
            "att_name": att_names,
            "sender": sender,
            "body": body,
        }
        scores = [(c, score_company(c, haystacks)) for c in companies]
        scores.sort(key=lambda x: -x[1])
        best_company, best_score = scores[0]
        if best_score == 0:
            return None, 0, "no match"
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
    print("Loading Dotterbolagslista ... ", end="", flush=True)
    companies = load_companies(DOTTERBOLAGSLISTA)
    id_index = {c["id"]: c for c in companies}
    print("{} active companies loaded.\n".format(len(companies)))

    msg_files = sorted(
        p for p in GET_TESTFILES_DIR.iterdir()
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
            label = "{:03d} {}".format(
                company["id"], (company["friendly"] or company["namn"]))
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
            print("    extracted/{}/  ->  {} mails".format(ctry, country_counts[ctry]))

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
                c["id"], fmt(c.get("country","Other"), 11),
                fmt(c["friendly"] or c["namn"], 30), name))

    if low_confidence:
        print()
        print("-- LOW CONFIDENCE --")
        for name, c, s in low_confidence:
            print("  score={:>3}  -> {:03d}  {}  {}  {}".format(
                s, c["id"], fmt(c.get("country","Other"), 11),
                fmt(c["friendly"] or c["namn"], 28), name))

    print()
    print("DRY RUN -- no files written.")


if __name__ == "__main__":
    main()
 low_confidence:
            print("  score={:>3}  -> {:03d}  {}  {}  {}".format(
                s, c["id"], fmt(c.get("country","Other"), 11),
                fmt(c["friendly"] or c["namn"], 28), name))

    print()
    print("DRY RUN -- no files written.")


if __name__ == "__main__":
    main()
