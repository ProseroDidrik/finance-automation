"""Läs counterparty_check_*.csv och bygg orgnr → bolag-mapping från SAF-T-filer.

Datakällor:
- `counterparty_check_{period}.csv` i repo-roten (skapas av check_counterparties.py)
- SAF-T-filer i `<base_path>/extracted/{period}/Norway/*_SAF-T_*.xml(|.zip)`

Mappingen byggs genom att parsa varje SAF-T och samla orgnr per fil. Filnamnet
har formatet `{ID:03d}_{FriendlyName}_{...}_SAF-T_{Year}-{Period}.xml` så
company_id härleds direkt från första 3 tecknen.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

# check_counterparties.py återanvänds för XML-parsning
import sys
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from check_counterparties import (  # noqa: E402
    extract_counterparties, find_saft_files, read_xml_bytes, SAFT_PATTERN,
)
from shared import load_config  # noqa: E402


_FILENAME_RE = re.compile(r"^(\d{3})_(.+?)_.+_SAF-T_\d{4}-\d+\.(xml|zip)$", re.IGNORECASE)


def _csv_path(period: str) -> Path:
    return REPO / f"counterparty_check_{period}.csv"


def list_available_periods() -> list[dict]:
    """Returnerar perioder med antingen CSV-rapport eller SAF-T-filer.

    [{period, has_csv, has_saft, n_saft_files}, ...]
    """
    periods: dict[str, dict] = {}

    # CSV:er i repo-roten
    for csvf in REPO.glob("counterparty_check_*.csv"):
        m = re.match(r"counterparty_check_(\d{6})\.csv$", csvf.name)
        if m:
            p = m.group(1)
            periods.setdefault(p, {"period": p, "has_csv": False, "has_saft": False, "n_saft_files": 0})
            periods[p]["has_csv"] = True

    # SAF-T-mappar
    cfg = load_config()
    base = Path(cfg["base_path"])
    extracted = base / "extracted"
    if extracted.exists():
        for d in extracted.iterdir():
            if d.is_dir() and re.match(r"^\d{6}$", d.name):
                p = d.name
                files = find_saft_files(d)
                if files:
                    periods.setdefault(p, {"period": p, "has_csv": False, "has_saft": False, "n_saft_files": 0})
                    periods[p]["has_saft"]      = True
                    periods[p]["n_saft_files"]  = len(files)

    return sorted(periods.values(), key=lambda r: r["period"], reverse=True)


def _company_from_filename(fname: str) -> tuple[int | None, str]:
    """`076_Aker_VN_SAF-T_2026-3.xml` → (76, 'Aker'). None om filnamnet inte matchar."""
    m = _FILENAME_RE.match(fname)
    if not m:
        return None, fname
    try:
        return int(m.group(1)), m.group(2).strip()
    except ValueError:
        return None, m.group(2).strip()


def build_orgnr_company_map(period: str, include_customers: bool = False) -> dict[str, list[dict]]:
    """orgnr → [{company_id, company_label, source_file}]. Parsar alla SAF-T-filer.

    En motpart kan finnas i flera bolag → returnerar lista.
    """
    cfg = load_config()
    base = Path(cfg["base_path"])
    period_dir = base / "extracted" / period
    files = find_saft_files(period_dir)

    out: dict[str, list[dict]] = {}
    for f in files:
        cid, label = _company_from_filename(f.name)
        try:
            xml_bytes = read_xml_bytes(f)
            if not xml_bytes:
                continue
            parties = extract_counterparties(xml_bytes, include_customers)
        except Exception:  # noqa: BLE001
            continue
        seen_in_file: set[str] = set()
        for p in parties:
            org = p.get("orgnr")
            if not org or org in seen_in_file:
                continue
            seen_in_file.add(org)
            out.setdefault(org, []).append({
                "company_id":    cid,
                "company_label": label,
                "source_file":   f.name,
            })
    return out


def _to_bool(s: str) -> bool:
    return s.strip().lower() in ("true", "1", "yes", "ja")


def read_counterparties(period: str) -> list[dict]:
    """Läser counterparty_check_{period}.csv → list[dict] med drilldown-data
    från SAF-T-parsning för bolag-mapping. Returnerar tom lista om CSV saknas.
    """
    p = _csv_path(period)
    if not p.exists():
        return []

    org_map = build_orgnr_company_map(period, include_customers=True)
    rows: list[dict] = []
    with p.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            orgnr = r.get("orgnr", "")
            companies = org_map.get(orgnr, [])
            # Dedup: en motpart kan vara både kund och leverantör i samma bolag
            seen = set()
            unique: list[dict] = []
            for c in companies:
                key = (c["company_id"], c["source_file"])
                if key in seen:
                    continue
                seen.add(key)
                unique.append(c)

            konkurs   = _to_bool(r.get("konkurs", ""))
            avveckling = _to_bool(r.get("under_avvikling", ""))
            tvangs    = _to_bool(r.get("tvangsavvikling", ""))
            sanctions = (r.get("sanctions_review") or "").strip()

            status = "ok"
            badges = []
            if konkurs:
                status = "flagged"; badges.append("KONKURS")
            if avveckling:
                status = "flagged"; badges.append("AVVECKLING")
            if tvangs:
                status = "flagged"; badges.append("TVANGS")
            if sanctions:
                status = "flagged"; badges.append("SANCTIONS")
            if r.get("brreg_found", "") == "False":
                badges.append("EJ I BRREG")

            rows.append({
                "orgnr":            orgnr,
                "type":             r.get("type", ""),
                "country":          r.get("country", "") or None,
                "name_saft":        r.get("name_saft", "") or None,
                "name_brreg":       r.get("name_brreg", "") or None,
                "brreg_found":      r.get("brreg_found", ""),
                "konkurs":          konkurs,
                "under_avvikling":  avveckling,
                "tvangsavvikling":  tvangs,
                "sanctions_review": sanctions or None,
                "status":           status,
                "badges":           badges,
                "companies":        unique,
                "source_file":      r.get("source_file", "") or None,
            })
    return rows
