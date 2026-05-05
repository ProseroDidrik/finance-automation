#!/usr/bin/env python3
"""
check_counterparties.py — Kontrollera leverantörer/kunder mot Brreg och sanktionslistor

Läser SAF-T XML-filer från extracted/{period}/Norway/ och slår upp varje unik
motpart mot:
  1. Brreg.no     — konkurs, underAvvikling, tvangsavvikling (öppet REST-API)
  2. sanctions.network — OFAC, EU och FN-sanktionslistor, opt-in (--with-sanctions)

Resultat sparas som CSV och visas i terminalen som [FLAGGED]/[OK]-rader.

Kör:
  py check_counterparties.py --period 202604
  py check_counterparties.py --period 202604 --with-sanctions
  py check_counterparties.py --period 202604 --include-customers
  py check_counterparties.py --period 202604 --dry-run
  py check_counterparties.py --file path/to/fil.xml
  py check_counterparties.py --period 202604 --out min_rapport.csv
  py check_counterparties.py --clear-cache   # töm Brreg-cache

Brreg-svar cachas i _params/brreg_cache.json för att undvika dubbelkoll.
"""

import argparse
import csv
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    import requests
except ImportError:
    sys.exit("Saknar requests — kör:  py -m pip install requests")

from shared import load_config, log, prev_month_period

# ── Konstanter ─────────────────────────────────────────────────────────────────
_BASE = Path(__file__).resolve().parent
GET_TESTFILES = Path(load_config()["base_path"])

BRREG_URL      = "https://data.brreg.no/enhetsregisteret/api/enheter/{}"
SANCTIONS_URL  = "https://api.sanctions.network/rpc/search_sanctions"
BRREG_CACHE_PATH = _BASE / "_params" / "brreg_cache.json"

SAFT_PATTERN = re.compile(r"^\d{3}_.+_SAF-T_\d{4}-\d+\.xml$")


# ── Cache ──────────────────────────────────────────────────────────────────────
def load_brreg_cache() -> dict:
    if BRREG_CACHE_PATH.exists():
        with open(BRREG_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_brreg_cache(cache: dict) -> None:
    BRREG_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = BRREG_CACHE_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    tmp.replace(BRREG_CACHE_PATH)


# ── XML-hjälpare ───────────────────────────────────────────────────────────────
def strip_ns(tag: str) -> str:
    return re.sub(r"\{[^}]+\}", "", tag)


def clean_orgnr(raw) -> str:
    """Returnerar siffror. Tom sträng om det inte är exakt 9 siffror."""
    digits = re.sub(r"[^0-9]", "", str(raw or ""))
    return digits if len(digits) == 9 else ""


def read_xml_bytes(path: Path):
    if path.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(path) as z:
                xmls = [n for n in z.namelist() if n.lower().endswith(".xml")]
                if not xmls:
                    return None
                with z.open(xmls[0]) as f:
                    return f.read()
        except zipfile.BadZipFile:
            return None
    return path.read_bytes()


def extract_counterparties(xml_bytes: bytes, include_customers: bool) -> list[dict]:
    """
    Returnerar lista av {type, orgnr, name, country} för Customer/Supplier-element
    som har ett giltigt 9-siffrigt org-nummer.
    """
    root = ET.fromstring(xml_bytes)
    results = []
    target_tags = {"Supplier"}
    if include_customers:
        target_tags.add("Customer")

    for elem in root.iter():
        tag = strip_ns(elem.tag)
        if tag not in target_tags:
            continue
        orgnr = name = country = ""
        for child in elem:
            ctag = strip_ns(child.tag)
            if ctag == "RegistrationNumber":
                orgnr = clean_orgnr(child.text)
            elif ctag == "Name" and not name:
                name = (child.text or "").strip()
            elif ctag == "Address":
                for addr_child in child:
                    if strip_ns(addr_child.tag) == "Country":
                        country = (addr_child.text or "").strip().upper()
        if orgnr:
            results.append({
                "type": tag.lower(),
                "orgnr": orgnr,
                "name": name,
                "country": country,
            })

    return results


# ── Brreg-koll ─────────────────────────────────────────────────────────────────
def fetch_brreg(orgnr: str, session: requests.Session) -> dict:
    try:
        resp = session.get(BRREG_URL.format(orgnr), timeout=10)
        if resp.status_code == 404:
            return {"found": False, "navn": "", "konkurs": False,
                    "underAvvikling": False, "tvangs": False}
        if resp.status_code == 200:
            d = resp.json()
            return {
                "found": True,
                "navn": d.get("navn", ""),
                "konkurs": bool(d.get("konkurs")),
                "underAvvikling": bool(d.get("underAvvikling")),
                "tvangs": bool(d.get("underTvangsavviklingEllerTvangsopplosning")),
            }
        return {"found": None, "navn": "", "konkurs": False,
                "underAvvikling": False, "tvangs": False,
                "error": f"HTTP {resp.status_code}"}
    except requests.RequestException as e:
        return {"found": None, "navn": "", "konkurs": False,
                "underAvvikling": False, "tvangs": False, "error": str(e)}


def check_brreg_batch(orgnrs: list[str], cache: dict) -> dict:
    """
    Slår upp alla org-nummer mot Brreg parallellt (10 trådar).
    Returnerar cache uppdaterad med nya svar.
    """
    to_fetch = [o for o in orgnrs if o not in cache]
    if not to_fetch:
        return cache

    session = requests.Session()
    session.headers["User-Agent"] = "finance-automation/1.0 (didrik.wachtmeister@gmail.com)"

    done = 0
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(fetch_brreg, orgnr, session): orgnr for orgnr in to_fetch}
        for future in as_completed(futures):
            orgnr = futures[future]
            cache[orgnr] = future.result()
            done += 1
            if done % 50 == 0 or done == len(to_fetch):
                print(f"  Brreg: {done}/{len(to_fetch)} uppslagna ...", flush=True)

    return cache


# ── Sanctions-koll ─────────────────────────────────────────────────────────────
def check_sanctions(name: str, session: requests.Session) -> list[dict]:
    """Returnerar lista av {source, names, remarks} för sanktionsträffar."""
    if not name:
        return []
    try:
        resp = session.get(SANCTIONS_URL, params={"name": name, "limit": "5"}, timeout=10)
        if resp.status_code != 200:
            return []
        return [
            {
                "source": h.get("source", ""),
                "names": h.get("names", []),
                "remarks": h.get("remarks", ""),
            }
            for h in resp.json()
        ]
    except requests.RequestException:
        return []


# ── Hitta SAF-T-filer ──────────────────────────────────────────────────────────
def find_saft_files(period_dir: Path) -> list[Path]:
    norway_dir = period_dir / "Norway"
    if not norway_dir.exists():
        return []
    return sorted(f for f in norway_dir.iterdir() if SAFT_PATTERN.match(f.name))


# ── Huvudlogik ─────────────────────────────────────────────────────────────────
def run(
    period: str,
    files: list[Path],
    output_path: Path,
    dry_run: bool,
    with_sanctions: bool,
    include_customers: bool,
) -> None:
    party_label = "kunder+leverantörer" if include_customers else "leverantörer"
    dry_label   = "  [DRY RUN]" if dry_run else ""
    sanctions_label = "  +sanctions" if with_sanctions else ""
    log("START", "check_counterparties.py",
        f"period {period}  {len(files)} fil(er)  {party_label}{sanctions_label}{dry_label}")

    # Samla alla motparter ur alla SAF-T-filer, dedup per orgnr
    all_parties: dict[str, dict] = {}  # orgnr → {type, orgnr, name, country, source_file}

    for saft_file in files:
        xml_bytes = read_xml_bytes(saft_file)
        if not xml_bytes:
            log("ERROR", saft_file.stem[:12], f"Kan inte läsa {saft_file.name}")
            continue
        try:
            parties = extract_counterparties(xml_bytes, include_customers)
        except ET.ParseError as e:
            log("ERROR", saft_file.stem[:12], f"XML-fel: {e}")
            continue

        new_count = 0
        for p in parties:
            if p["orgnr"] not in all_parties:
                all_parties[p["orgnr"]] = {**p, "source_file": saft_file.name}
                new_count += 1
            else:
                # Kombinera type om samma org dyker upp som both
                existing = all_parties[p["orgnr"]]
                if p["type"] not in existing["type"]:
                    existing["type"] = existing["type"] + "/" + p["type"]

        log("INFO", saft_file.stem[:12],
            f"{len(parties)} motparter  ({new_count} nya unika org-nr)")

    log("INFO", "check_counterparties",
        f"{len(all_parties)} unika org-nummer att kontrollera")

    if dry_run:
        print(f"\n{'ORG-NR':<12}  {'TYP':<12}  NAMN")
        print("-" * 60)
        for orgnr, p in sorted(all_parties.items()):
            print(f"{orgnr:<12}  {p['type']:<12}  {p['name']}")
        log("DONE", "check_counterparties.py",
            f"[DRY] {len(all_parties)} motparter listade, inga API-anrop")
        return

    # ── Brreg-uppslag (med cache) ──────────────────────────────────────────────
    cache = load_brreg_cache()
    cache_before = len(cache)
    print(f"\nBrreg: {len(all_parties)} orgnr att kolla ({cache_before} redan cachade)...")
    cache = check_brreg_batch(list(all_parties.keys()), cache)
    new_cached = len(cache) - cache_before
    if new_cached > 0:
        save_brreg_cache(cache)
        print(f"  Cache uppdaterad: +{new_cached} nya poster")

    # ── Sanctions-uppslag ──────────────────────────────────────────────────────
    sanctions_results: dict[str, list] = {}
    if with_sanctions:
        print(f"\nSanctions: kontrollerar {len(all_parties)} namn...")
        san_session = requests.Session()
        san_session.headers["User-Agent"] = \
            "finance-automation/1.0 (didrik.wachtmeister@gmail.com)"
        done = 0
        for orgnr, p in all_parties.items():
            brreg_rec = cache.get(orgnr, {})
            check_name = brreg_rec.get("navn") or p["name"]
            hits = check_sanctions(check_name, san_session)
            if hits:
                sanctions_results[orgnr] = hits
            done += 1
            if done % 50 == 0 or done == len(all_parties):
                print(f"  Sanctions: {done}/{len(all_parties)} ...", flush=True)
            time.sleep(0.05)

    # ── Bygg CSV-rader ─────────────────────────────────────────────────────────
    rows = []
    flagged_brreg = 0
    flagged_sanctions = 0

    for orgnr, p in sorted(all_parties.items()):
        br = cache.get(orgnr, {})
        san_hits = sanctions_results.get(orgnr, [])

        brreg_flag = br.get("konkurs") or br.get("underAvvikling") or br.get("tvangs")
        sanctions_flag = bool(san_hits)
        if brreg_flag:
            flagged_brreg += 1
        if sanctions_flag:
            flagged_sanctions += 1

        sanctions_str = "; ".join(
            f"{h['source'].upper()}: {h['names'][0]}" for h in san_hits if h.get("names")
        )

        rows.append({
            "orgnr":          orgnr,
            "type":           p["type"],
            "country":        p["country"],
            "name_saft":      p["name"],
            "name_brreg":     br.get("navn", ""),
            "brreg_found":    br.get("found", ""),
            "konkurs":        br.get("konkurs", ""),
            "under_avvikling": br.get("underAvvikling", ""),
            "tvangsavvikling": br.get("tvangs", ""),
            "brreg_flagged":  bool(brreg_flag),
            "sanctions_review": sanctions_str,
            "source_file":    p["source_file"],
        })

        if brreg_flag or sanctions_flag:
            status_parts = []
            if br.get("konkurs"):
                status_parts.append("KONKURS")
            if br.get("underAvvikling"):
                status_parts.append("AVVIKLING")
            if br.get("tvangs"):
                status_parts.append("TVANGS")
            if sanctions_flag:
                status_parts.append(f"SANCTIONS({len(san_hits)})")
            log("WARN", orgnr, f"{br.get('navn') or p['name']}  [{', '.join(status_parts)}]")

    # ── Skriv CSV ──────────────────────────────────────────────────────────────
    fieldnames = [
        "orgnr", "type", "country", "name_saft", "name_brreg",
        "brreg_found", "konkurs", "under_avvikling", "tvangsavvikling",
        "brreg_flagged", "sanctions_review", "source_file",
    ]
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    log("OK", "check_counterparties",
        f"Rapport sparad: {output_path.name}  ({len(rows)} rader)")
    log("DONE", "check_counterparties.py",
        f"{len(rows)} kontrollerade  "
        f"{flagged_brreg} Brreg-flaggade  "
        f"{flagged_sanctions} sanctions-träffar (kräver granskning)")


# ── CLI ────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Kontrollera SAF-T-motparter mot Brreg och sanktionslistor"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--period", metavar="YYYYMM",
        help="Period att köra, t.ex. 202604 (läser extracted/{period}/Norway/)",
    )
    group.add_argument(
        "--file", metavar="XML",
        help="Kontrollera en enskild SAF-T XML-fil",
    )
    group.add_argument(
        "--clear-cache", action="store_true",
        help="Töm Brreg-cachen och avsluta",
    )
    parser.add_argument(
        "--out", metavar="CSV",
        help="Sökväg till CSV-rapporten (default: counterparty_check_{period}.csv)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Lista motparter utan API-anrop",
    )
    parser.add_argument(
        "--with-sanctions", action="store_true",
        help="Kör även namnsökning mot OFAC/EU/FN-sanktionslistor",
    )
    parser.add_argument(
        "--include-customers", action="store_true",
        help="Inkludera kunder (standard: bara leverantörer)",
    )
    args = parser.parse_args()

    if args.clear_cache:
        if BRREG_CACHE_PATH.exists():
            BRREG_CACHE_PATH.unlink()
            print(f"Cache raderad: {BRREG_CACHE_PATH}")
        else:
            print("Ingen cache att radera.")
        return

    if args.period:
        period = args.period
        period_dir = GET_TESTFILES / "extracted" / period
        files = find_saft_files(period_dir)
        if not files:
            log("ERROR", "check_counterparties",
                f"Inga SAF-T-filer hittades i extracted/{period}/Norway/")
            sys.exit(1)
    else:
        p = Path(args.file)
        if not p.exists():
            log("ERROR", "check_counterparties", f"Filen hittades inte: {args.file}")
            sys.exit(1)
        files = [p]
        period = prev_month_period()

    output_path = Path(args.out) if args.out else _BASE / f"counterparty_check_{period}.csv"

    run(
        period=period,
        files=files,
        output_path=output_path,
        dry_run=args.dry_run,
        with_sanctions=args.with_sanctions,
        include_customers=args.include_customers,
    )


if __name__ == "__main__":
    main()
