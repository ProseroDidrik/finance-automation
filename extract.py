# -*- coding: utf-8 -*-
"""
Extract attachments from .msg files in _inbox/, organised into country subfolders.
Output: extracted/{Country}/{ID:03d}_{originalname}
"""
from __future__ import annotations
import re
from pathlib import Path
import extract_msg
import openpyxl

from shared import load_config as _load_config

_BASE          = Path(__file__).resolve().parent
GET_TESTFILES  = Path(_load_config()["base_path"])
INBOX_DIR      = GET_TESTFILES / "_inbox"
OUT_DIR        = GET_TESTFILES / "extracted"
DOTTERBOLAG    = _BASE / "_params" / "Dotterbolagslista.xlsx"

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
    "Doorway mars": 162,               # subject "Doorway mars", matchade fel mot 004 Prosero
    "SIE Montageservice": 94,          # subject "SIE Montageservice", matchade fel mot 012 (samma open-up.se-grupp)
    "SIE-fil Dala Lås i Ludvika AB 2603": 73,  # filnamn säger Ludvika, matchade fel mot 072 (samma dalalås.se-grupp)
    "Låskomfort mars": 88,             # filnamn "LÅSKOMFO", matchade fel mot 087 (samma sicklalasteknik.se-grupp)
    "Månadsavstämning 2026-03-31 (2)": 93,     # bilaga Hässleholm, matchade fel mot 097 (samma hlmlassmed.se-grupp)
    "VB_ Prosero Securty Oy": 145,             # stavfel "Securty" i originalfilnamnet; annars matchad till 052
}

# Per-bilaga-overrides: (msg_stem, lowercase-delsträng i bilagans filnamn) -> bolag_id
# Används när ett och samma mail innehåller bilagor för flera bolag (t.ex. samma redovisningsbyrå).
ATTACHMENT_OVERRIDES = {
    ("Nylunds och Norrskydd mars", "norrskydd"): 76,   # AB Norrskydd
    ("Nylunds och Norrskydd mars", "nylunds"):   183,  # Nylunds Lås & Larm
}

# Interna bolag vars Market-kolumn (C) i Dotterbolagslista inte matchar något KNOWN_COUNTRIES-värde.
COUNTRY_OVERRIDES: dict[int, str] = {
    49:  "Sweden",
    50:  "Sweden",
    51:  "Sweden",
    53:  "Sweden",
    60:  "Sweden",
    162: "Sweden",
    52:  "Norway",
    54:  "Denmark",
    145: "Finland",
    187: "Germany",
}

INLINE_IMAGE_RE = re.compile(
    r"^(image\d+\.(png|gif|jpg|jpeg|bmp)|img-[0-9a-f\-]{30,})$", re.I
)
UNSAFE_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize(name):
    name = UNSAFE_FS_CHARS.sub("_", name)
    return name.strip(" .") or "unnamed"


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


def unique_path(p):
    if not p.exists():
        return p
    stem, ext = p.stem, p.suffix
    n = 2
    while True:
        c = p.with_name("{} ({}){}".format(stem, n, ext))
        if not c.exists():
            return c
        n += 1


def load_companies(path):
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    ws = wb["Data For Company Find"]
    companies = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or row[1] is None:
            continue
        if (row[7] or "").strip().lower() == "consolidated":
            continue
        bolag_id = int(row[1])
        namn = re.sub(r"^\s*\d+\s*", "", str(row[0] or "").strip())
        friendly = str(row[4] or "").strip()
        doman = str(row[9] or "").strip() if len(row) > 9 else ""
        market_raw = str(row[2] or "").strip()
        country = market_raw if market_raw in KNOWN_COUNTRIES else "Other"
        if bolag_id in COUNTRY_OVERRIDES:
            country = COUNTRY_OVERRIDES[bolag_id]
        companies.append({
            "id": bolag_id,
            "namn": namn,
            "friendly": friendly,
            "doman": doman,
            "country": country,
            "tokens": list({t for s in (namn, friendly) for t in tokenize(s)}),
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
        return id_index.get(override_id, {
            "id": override_id, "friendly": "", "namn": "ID {}".format(override_id),
            "country": "Other", "tokens": [], "doman": "",
        }), 999
    try:
        msg = extract_msg.Message(str(msg_path))
    except Exception as e:
        return None, 0
    try:
        att_names = " ".join(att_name(a, i) for i, a in enumerate(msg.attachments) if not is_inline(a))
        haystacks = {
            "filename": msg_path.stem,
            "subject": msg.subject or "",
            "att_name": att_names,
            "sender": msg.sender or "",
            "body": (msg.body or "")[:1500],
        }
        scores = [(c, score_company(c, haystacks)) for c in companies]
        scores.sort(key=lambda x: -x[1])
        best_c, best_s = scores[0]
        return (best_c, best_s) if best_s > 0 else (None, 0)
    finally:
        try:
            msg.close()
        except Exception:
            pass


def main():
    print("Loading Dotterbolagslista ... ", end="", flush=True)
    companies = load_companies(DOTTERBOLAG)
    id_index = {c["id"]: c for c in companies}
    print("{} companies.\n".format(len(companies)))

    msg_files = sorted(p for p in INBOX_DIR.iterdir() if p.is_file() and p.suffix.lower() == ".msg")
    print("Found {} .msg files in _inbox/\n".format(len(msg_files)))

    # Create country subdirs
    for ctry in list(KNOWN_COUNTRIES) + ["Other"]:
        (OUT_DIR / ctry).mkdir(parents=True, exist_ok=True)

    total_saved = 0
    total_inline = 0
    total_no_att = 0
    failed = []

    for i, msg_path in enumerate(msg_files, 1):
        company, score = match_msg(msg_path, companies, id_index)
        if company is None:
            print("[{:>3}/{}] NO MATCH: {}".format(i, len(msg_files), msg_path.name))
            failed.append(msg_path.name)
            continue

        country = company.get("country", "Other")
        prefix = "{:03d}_".format(company["id"])
        out_subdir = OUT_DIR / country
        score_str = "MAN" if score == 999 else str(score)

        try:
            msg = extract_msg.Message(str(msg_path))
        except Exception as e:
            print("[{:>3}/{}] FAILED to open: {} ({})".format(i, len(msg_files), msg_path.name, e))
            failed.append(msg_path.name)
            continue

        saved_here = 0
        inline_here = 0
        try:
            for idx, att in enumerate(msg.attachments):
                if is_inline(att):
                    inline_here += 1
                    continue
                orig = att_name(att, idx)
                safe = sanitize(orig)

                # Per-bilaga-override: kolla om detta (msg_stem, bilagenamn) matchar
                att_company = company
                for (stem_key, att_substr), att_id in ATTACHMENT_OVERRIDES.items():
                    if msg_path.stem == stem_key and att_substr in orig.lower():
                        att_company = id_index.get(att_id, {
                            "id": att_id, "friendly": "", "namn": "ID {}".format(att_id),
                            "country": "Other", "tokens": [], "doman": "",
                        })
                        break
                att_prefix   = "{:03d}_".format(att_company["id"])
                att_subdir   = OUT_DIR / att_company.get("country", "Other")
                att_subdir.mkdir(parents=True, exist_ok=True)

                target = unique_path(att_subdir / "{}{}".format(att_prefix, safe))
                data = att.data
                if data is None:
                    inline_here += 1
                    continue
                if isinstance(data, (bytes, bytearray)):
                    target.write_bytes(data)
                else:
                    try:
                        att.save(customPath=str(att_subdir), customFilename="{}{}".format(att_prefix, safe))
                    except Exception as e:
                        failed.append("{} / {}".format(msg_path.name, safe))
                        continue
                saved_here += 1

            total_saved += saved_here
            total_inline += inline_here
            if saved_here == 0:
                total_no_att += 1
            label = (company["friendly"] or company["namn"])[:28]
            print("[{:>3}/{}] score={:>3}  {:03d} {}  {} att  ->  {}/".format(
                i, len(msg_files), score_str, company["id"], label.ljust(28),
                saved_here, country))
        finally:
            try:
                msg.close()
            except Exception:
                pass

    print()
    print("=" * 80)
    print("  Emails processed : {}".format(len(msg_files)))
    print("  Attachments saved: {}".format(total_saved))
    print("  No-attachment mail: {}".format(total_no_att))
    print("  Inline skipped   : {}".format(total_inline))
    print("  Failed           : {}".format(len(failed)))
    for f in failed:
        print("    - {}".format(f))
    print()
    print("Done. Files are in: {}".format(OUT_DIR))


if __name__ == "__main__":
    main()
