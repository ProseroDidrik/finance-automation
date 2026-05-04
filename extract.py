# -*- coding: utf-8 -*-
"""
Extract attachments from .msg files in _inbox/, organised into country subfolders.
Output: extracted/{Country}/{ID:03d}_{originalname}
"""
from __future__ import annotations
import argparse
import re
import sys
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
OUT_DIR       = GET_TESTFILES / "extracted"
DOTTERBOLAG   = _BASE / "_params" / "Dotterbolagslista.xlsx"


def _prev_month_period() -> str:
    today = date.today()
    if today.month == 1:
        return f"{today.year - 1}12"
    return f"{today.year}{today.month - 1:02d}"

KNOWN_COUNTRIES = ("Sweden", "Norway", "Finland", "Denmark", "Germany")

# Overrides läses från _params/overrides.json (editerbar via GUI:t).
# OVERRIDES: msg_path.stem → bolag_id (subject-level)
# ATTACHMENT_OVERRIDES: (msg_stem, lowercase-delsträng i bilagans filnamn) → bolag_id
# COUNTRY_OVERRIDES: bolag_id → land (för interna bolag vars Market-kolumn inte matchar)
_OV = _load_overrides()
OVERRIDES: dict[str, int] = {k: int(v) for k, v in _OV["subject_overrides"].items()}
ATTACHMENT_OVERRIDES: dict[tuple[str, str], int] = {
    (item["msg_stem"], item["attachment_substr"]): int(item["bolag_id"])
    for item in _OV["attachment_overrides"]
}
COUNTRY_OVERRIDES: dict[int, str] = {int(k): v for k, v in _OV["country_overrides"].items()}


def _build_alias_index(raw: dict) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for k, phrases in raw.items():
        try:
            bid = int(k)
        except (TypeError, ValueError):
            continue
        cleaned = [normalize(p) for p in phrases if p and p.strip()]
        cleaned = [p for p in cleaned if p]
        if cleaned:
            out[bid] = cleaned
    return out

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


# Bolagssuffix som inte diskriminerar — finns i nästan alla bolagsnamn och
# blockerar annars full-match när suffixet inte råkar finnas i mailet.
TOKEN_STOPWORDS = {"ab", "oy", "oyj", "as", "aps", "ltd", "gmbh", "ag", "ry", "inc", "plc", "llc"}


def tokenize(s):
    return [t for t in normalize(s).split() if len(t) >= 2 and t not in TOKEN_STOPWORDS]


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
            "aliases": ALIASES_BY_ID.get(bolag_id, []),
        })
    wb.close()
    return companies


ALIASES_BY_ID: dict[int, list[str]] = _build_alias_index(_OV.get("aliases", {}))

WEIGHTS = {"filename": 100, "subject": 80, "att_name": 60, "sender": 40, "body": 20}

# Aliases är manuellt kurerade signaler — en träff är stark intent oavsett källa.
# Floor på 50 låter alias i body slå pure sender-match (40).
ALIAS_MIN_WEIGHT = 50


def score_company(company, haystacks):
    best = 0
    tokens = company["tokens"]
    aliases = company.get("aliases", [])
    if not tokens and not aliases:
        return 0
    for source, weight in WEIGHTS.items():
        hay = normalize(haystacks.get(source, ""))
        if not hay:
            continue
        full_match = bool(tokens) and all(t in hay for t in tokens)
        alias_match = any(a in hay for a in aliases)
        if full_match:
            best = max(best, weight)
        if alias_match:
            best = max(best, max(weight, ALIAS_MIN_WEIGHT))
        if not (full_match or alias_match) and tokens:
            matched = sum(1 for t in tokens if t in hay)
            if matched:
                best = max(best, int(weight * matched / len(tokens) * 0.6))
    if company["doman"] and company["doman"] in haystacks.get("sender", ""):
        best = max(best, WEIGHTS["sender"])
    return best


def match_msg(msg_path, companies, id_index):
    stem_key = msg_path.stem.strip()
    if stem_key in OVERRIDES:
        override_id = OVERRIDES[stem_key]
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
        allowed = _country_constraint(haystacks)
        eligible = (
            [c for c in companies if c["country"] in allowed]
            if allowed else companies
        )
        if not eligible:
            return (None, 0)
        sender = haystacks.get("sender", "")
        domain_matches = [c["id"] for c in eligible if c["doman"] and c["doman"] in sender]
        unique_sender_id = domain_matches[0] if len(domain_matches) == 1 else None
        scores = [(c, score_company(c, haystacks)) for c in eligible]
        scores.sort(key=lambda x: (
            -x[1],
            -len(x[0]["tokens"]),
            -int(x[0]["id"] == unique_sender_id),
        ))
        best_c, best_s = scores[0]
        return (best_c, best_s) if best_s > 0 else (None, 0)
    finally:
        try:
            msg.close()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Extrahera bilagor från .msg-filer")
    parser.add_argument(
        "--period", "-p", metavar="YYYYMM", default=None,
        help="Period att köra (t.ex. 202603). Standard: föregående månad.",
    )
    args = parser.parse_args()
    period = args.period or _prev_month_period()
    _begin_run("extract", period)
    inbox_dir = GET_TESTFILES / "_inbox" / period

    if not inbox_dir.exists():
        sys.exit(
            f"Inbox-mapp saknas: {inbox_dir}\n"
            f"Skapa mappen och lägg .msg-filerna för period {period} där."
        )

    print("Period  : {}".format(period))
    print("Inbox   : {}".format(inbox_dir))
    print("Loading Dotterbolagslista ... ", end="", flush=True)
    companies = load_companies(DOTTERBOLAG)
    id_index = {c["id"]: c for c in companies}
    print("{} companies.\n".format(len(companies)))

    msg_files = sorted(p for p in inbox_dir.iterdir() if p.is_file() and p.suffix.lower() == ".msg")
    print("Found {} .msg files in _inbox/{}/\n".format(len(msg_files), period))

    # Create country subdirs under period
    for ctry in list(KNOWN_COUNTRIES) + ["Other"]:
        (OUT_DIR / period / ctry).mkdir(parents=True, exist_ok=True)

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
        out_subdir = OUT_DIR / period / country
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
                stem_lower = msg_path.stem.strip().lower()
                for (stem_key, att_substr), att_id in ATTACHMENT_OVERRIDES.items():
                    if stem_key.lower() in stem_lower and att_substr in orig.lower():
                        att_company = id_index.get(att_id, {
                            "id": att_id, "friendly": "", "namn": "ID {}".format(att_id),
                            "country": "Other", "tokens": [], "doman": "",
                        })
                        break
                att_prefix   = "{:03d}_".format(att_company["id"])
                att_subdir   = OUT_DIR / period / att_company.get("country", "Other")
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
