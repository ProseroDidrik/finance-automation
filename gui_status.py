"""Status-derivering för GUI:t.

Kombinerar Dotterbolagslistan + filsystem (extracted/output/Referens) + JSONL-loggar
till en lista CompanyRow per bolag för en given period.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from shared import load_config, load_dotterbolag_full, load_overrides

REPO_ROOT = Path(__file__).resolve().parent
DOTTERBOLAG_PATH = REPO_ROOT / "_params" / "Dotterbolagslista.xlsx"
LOGS_DIR = REPO_ROOT / "_logs"

KNOWN_COUNTRIES = ("Sweden", "Norway", "Finland", "Denmark", "Germany")


@dataclass
class CompanyRow:
    bolag_id: int
    country: str
    name: str
    extracted: bool = False
    dry_run_matched: bool = False
    processed: bool = False
    excluded: bool = False
    output_files: list[str] = field(default_factory=list)
    extracted_files: list[str] = field(default_factory=list)
    referens_files: list[str] = field(default_factory=list)
    last_status: str | None = None
    last_msg: str = ""
    events: list[dict] = field(default_factory=list)


def base_path() -> Path:
    return Path(load_config()["base_path"])


def country_dir(period: str, country: str) -> Path:
    return base_path() / "extracted" / period / country


def available_periods() -> list[str]:
    """Periods som syns i extracted/ eller _logs/, sorterade fallande."""
    periods: set[str] = set()
    extracted_root = base_path() / "extracted"
    if extracted_root.exists():
        for p in extracted_root.iterdir():
            if p.is_dir() and p.name.isdigit() and len(p.name) == 6:
                periods.add(p.name)
    if LOGS_DIR.exists():
        for p in LOGS_DIR.iterdir():
            if p.is_dir() and p.name.isdigit() and len(p.name) == 6:
                periods.add(p.name)
    return sorted(periods, reverse=True)


def _read_jsonl_events(period: str) -> list[dict]:
    """Alla events från _logs/{period}/*.jsonl, sorterade efter ts."""
    period_dir = LOGS_DIR / period
    events: list[dict] = []
    if not period_dir.exists():
        return events
    for jsonl in period_dir.glob("*.jsonl"):
        try:
            with open(jsonl, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
    events.sort(key=lambda e: e.get("ts", ""))
    return events


def _resolve_country(bolag_id: int, market: str, country_overrides: dict[int, str]) -> str:
    if bolag_id in country_overrides:
        return country_overrides[bolag_id]
    return market if market in KNOWN_COUNTRIES else "Other"


def _find_files_with_prefix(directory: Path, prefix: str) -> list[str]:
    if not directory.exists():
        return []
    return sorted(p.name for p in directory.iterdir() if p.is_file() and p.name.startswith(prefix))


def _latest_dry_run_matches(events: list[dict]) -> set[int]:
    """Return bolag-IDs som matchades i SENASTE dry_run-körningen.

    Förlitar sig på att dry_run.py:
      1) skriver en START-rad via begin_run() vid uppstart, och
      2) skriver en MATCH-rad per matchad .msg via log_event().
    Tar events efter senaste dry_run-START och plockar MATCH-labels.
    """
    last_start_idx = -1
    for i, ev in enumerate(events):
        if ev.get("script") == "dry_run" and ev.get("status") == "START":
            last_start_idx = i
    if last_start_idx < 0:
        return set()
    matched: set[int] = set()
    for ev in events[last_start_idx + 1:]:
        if ev.get("script") != "dry_run" or ev.get("status") != "MATCH":
            continue
        label = str(ev.get("label", ""))
        if label.isdigit():
            matched.add(int(label))
    return matched


def compute_company_status(period: str) -> list[CompanyRow]:
    """Returnera en CompanyRow per bolag i Dotterbolagslistan för given period.

    Skippar consolidated-rader och bolag utan känt land. Sorteras (country, bolag_id).
    """
    full = load_dotterbolag_full(DOTTERBOLAG_PATH)
    ov = load_overrides()
    country_overrides = {int(k): v for k, v in ov.get("country_overrides", {}).items()}
    excluded_ids: set[int] = {
        int(i) for i in ov.get("excluded", []) if str(i).strip().lstrip("-").isdigit()
    }

    events = _read_jsonl_events(period)
    events_by_label: dict[str, list[dict]] = {}
    for ev in events:
        events_by_label.setdefault(ev.get("label", ""), []).append(ev)
    dry_run_matched = _latest_dry_run_matches(events)

    rows: list[CompanyRow] = []
    base = base_path() / "extracted" / period

    for bolag_id, meta in full.items():
        if meta.get("kind", "").lower() == "consolidated":
            continue
        country = _resolve_country(bolag_id, meta.get("country", ""), country_overrides)
        # Visa även "Other"-bolag (Market-kolumnen i Dotterbolagslistan matchade
        # inte ett känt land och inget COUNTRY_OVERRIDES-värde fanns).

        prefix = f"{bolag_id:03d}_"
        country_dir_p = base / country
        extracted_files = _find_files_with_prefix(country_dir_p, prefix)
        referens_files = _find_files_with_prefix(country_dir_p / "Referens", prefix)
        output_files = _find_files_with_prefix(country_dir_p / "output", prefix)

        company_events = events_by_label.get(str(bolag_id), [])
        # Hoppa över MATCH-events från dry_run vid val av "senaste meddelande" — de
        # är feedback från extract-matchningen, inte process-status, och skulle annars
        # alltid skriva över process-resultatet i tabellen efter en dry-run.
        non_match_events = [
            ev for ev in company_events
            if not (ev.get("script") == "dry_run" and ev.get("status") == "MATCH")
        ]
        last_event = non_match_events[-1] if non_match_events else None

        rows.append(CompanyRow(
            bolag_id=bolag_id,
            country=country,
            name=meta.get("name", ""),
            extracted=bool(extracted_files) or bool(referens_files),
            dry_run_matched=bolag_id in dry_run_matched,
            processed=bool(referens_files) or bool(output_files),
            excluded=bolag_id in excluded_ids,
            output_files=output_files,
            extracted_files=extracted_files,
            referens_files=referens_files,
            last_status=last_event["status"] if last_event else None,
            last_msg=last_event["msg"] if last_event else "",
            events=company_events,
        ))

    country_order = {c: i for i, c in enumerate(KNOWN_COUNTRIES)}
    rows.sort(key=lambda r: (country_order.get(r.country, len(KNOWN_COUNTRIES)), r.bolag_id))
    return rows


def run_level_events(period: str) -> list[dict]:
    """Events där label är ett script-namn (START/DONE), inte en bolags-id."""
    return [e for e in _read_jsonl_events(period) if not e.get("label", "").isdigit()]
