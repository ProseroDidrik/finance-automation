"""Shared utilities for finance-automation country processing scripts."""
import json
import re
from datetime import date, datetime
from pathlib import Path
import shutil
import sys

DUPE_RE = re.compile(r"\s\(([2-9]|\d{2,})\)\.\w+$")

# Filformats-baserade landsbegränsningar för mail-matchning.
# SIE-filer är ett svenskt redovisningsformat → kan endast gälla svenska bolag.
# SAF-T används i denna pipeline för norska och danska bolag.
_SIE_RE  = re.compile(r"\.sie\b|\bsie[- ]?fil|\bsiefil", re.IGNORECASE)
_SAFT_RE = re.compile(r"\bsaf[- ]?t\b", re.IGNORECASE)


def country_constraint_from_haystacks(haystacks: dict) -> tuple[str, ...] | None:
    """Returnera tillåtna länder baserat på filformatssignal i mailet.

    Inspekterar filename + subject + att_name (inte body/sender — för noisy).
    SIE-signal → ('Sweden',). SAF-T-signal → ('Norway', 'Denmark'). Annars None.
    """
    text = " ".join((
        haystacks.get("filename", ""),
        haystacks.get("subject", ""),
        haystacks.get("att_name", ""),
    ))
    if _SIE_RE.search(text):
        return ("Sweden",)
    if _SAFT_RE.search(text):
        return ("Norway", "Denmark")
    return None

_REPO_ROOT = Path(__file__).resolve().parent
_run_ctx: dict = {"script": None, "period": None, "log_path": None}


def prev_month_period() -> str:
    """Return YYYYMM for the previous calendar month (handles January wrap)."""
    today = date.today()
    if today.month == 1:
        return f"{today.year - 1}12"
    return f"{today.year}{today.month - 1:02d}"


def begin_run(script_name: str, period: str) -> None:
    """Activate JSONL persistence for subsequent log() calls in this process.

    Writes to _logs/{period}/{script_name}.jsonl. Idempotent within a process.
    Terminal output via log() is unaffected; only adds a side-effect write.
    """
    log_dir = _REPO_ROOT / "_logs" / period
    log_dir.mkdir(parents=True, exist_ok=True)
    _run_ctx["script"] = script_name
    _run_ctx["period"] = period
    _run_ctx["log_path"] = log_dir / f"{script_name}.jsonl"
    _append_event("START", script_name, f"period {period}")


def _append_event(status: str, label, msg: str) -> None:
    if not _run_ctx["log_path"]:
        return
    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "script": _run_ctx["script"],
        "period": _run_ctx["period"],
        "status": status,
        "label": str(label),
        "msg": msg,
    }
    try:
        with open(_run_ctx["log_path"], "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass

try:
    import openpyxl
except ImportError:
    sys.exit("Saknar openpyxl — kör:  py -m pip install openpyxl")

try:
    import pandas as pd
except ImportError:
    sys.exit("Saknar pandas — kör:  py -m pip install pandas openpyxl")


def load_config() -> dict:
    """Load base_path (and other settings) from config.json in the repo root."""
    config_path = Path(__file__).parent / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(
            "config.json saknas. Skapa den i repo-roten med innehållet:\n"
            '  {"base_path": "C:\\\\...\\\\Get testfiles"}'
        )
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def log(status: str, label, msg: str = "") -> None:
    """Print a structured log line: [STATUS]  label  msg

    If begin_run() has been called in this process, also append a JSONL event
    to _logs/{period}/{script}.jsonl so a GUI can read run history later.
    """
    tag = f"[{status}]"
    line = f"{tag:<8} {label}"
    if msg:
        line += f"  {msg}"
    print(line)
    _append_event(status, label, msg)


def log_event(status: str, label, msg: str = "") -> None:
    """Append a JSONL event without printing. Useful for events that should be
    visible to the GUI (via _logs/*.jsonl) but not clutter the script's stdout."""
    _append_event(status, label, msg)


def load_dotterbolag(path: Path) -> dict[int, str]:
    """bolagsid → friendly name from Dotterbolagslistan, skips 'consolidated' rows."""
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    ws = wb["Data For Company Find"]
    result: dict[int, str] = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or len(row) < 5:
            continue
        bolag_id = row[1]
        friendly = row[4]
        kind = row[7] if len(row) > 7 else None
        if str(kind).strip().lower() == "consolidated":
            continue
        if bolag_id and friendly:
            result[int(bolag_id)] = str(friendly).strip()
    wb.close()
    return result


def load_dotterbolag_full(path: Path) -> dict[int, dict]:
    """bolagsid → {name, country, orgnr, domain, kind} from Dotterbolagslistan.

    Includes 'consolidated' rows (caller filters if needed). Used by the GUI
    where Country (col C) is needed in addition to friendly name.
    """
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    ws = wb["Data For Company Find"]
    result: dict[int, dict] = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or len(row) < 2 or row[1] is None:
            continue
        try:
            bolag_id = int(row[1])
        except (TypeError, ValueError):
            continue
        result[bolag_id] = {
            "name": str(row[4]).strip() if len(row) > 4 and row[4] else "",
            "country": str(row[2]).strip() if len(row) > 2 and row[2] else "",
            "orgnr": str(row[5]).strip() if len(row) > 5 and row[5] else "",
            "kind": str(row[7]).strip() if len(row) > 7 and row[7] else "",
            "domain": str(row[9]).strip() if len(row) > 9 and row[9] else "",
        }
    wb.close()
    return result


def load_overrides() -> dict:
    """Load _params/overrides.json (subject/attachment/country/alias overrides for extract).

    Returns a dict with keys: subject_overrides (dict[str, int]),
    attachment_overrides (list of {msg_stem, attachment_substr, bolag_id}),
    country_overrides (dict[str, str]),
    aliases (dict[str, list[str]] — bolag_id → phrases that score full weight when
    found as substring in any haystack source). Empty defaults if file missing.
    """
    p = _REPO_ROOT / "_params" / "overrides.json"
    if not p.exists():
        return {
            "subject_overrides": {}, "attachment_overrides": [],
            "country_overrides": {}, "aliases": {},
        }
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("subject_overrides", {})
    data.setdefault("attachment_overrides", [])
    data.setdefault("country_overrides", {})
    data.setdefault("aliases", {})
    return data


def safe_dest(dest: Path) -> Path:
    """Return a unique path: append _2, _3, ... if dest already exists."""
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    i = 2
    while True:
        candidate = dest.parent / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def move_to_referens_safe(src: Path, referens_dir: Path, dry_run: bool) -> Path:
    """Move src into referens_dir, avoiding filename collisions. Returns actual destination."""
    dst = safe_dest(referens_dir / src.name)
    if dry_run:
        print(f"    [dry] → Referens/{dst.name}")
    else:
        referens_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        print(f"    → Referens/{dst.name}")
    return dst


def glob_one(directory: Path, pattern: str) -> Path:
    """Return the best-matching file for pattern in directory.
    Prefers non-duplicate files (unique_path-created copies have ` (2)+` suffix);
    falls back to first match if all matches look like dupes."""
    matches = sorted(directory.glob(pattern))
    if not matches:
        raise FileNotFoundError(directory / pattern)
    non_dupes = [f for f in matches if not DUPE_RE.search(f.name)]
    return non_dupes[0] if non_dupes else matches[0]


def save_inl_xlsx(is_rows: list, bs_rows: list, output_path: Path) -> None:
    """Write IS+BS rows to INL.xlsx (empty row 1, then data rows with cols A/B/C)."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    records = [{"A": None, "B": None, "C": None}]
    for acc, name, amt in is_rows + bs_rows:
        records.append({"A": acc, "B": name, "C": amt})
    df = pd.DataFrame(records)
    with pd.ExcelWriter(str(output_path), engine="openpyxl") as writer:
        df.to_excel(writer, index=False, header=False, sheet_name="Sheet1")
