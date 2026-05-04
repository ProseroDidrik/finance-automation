"""Ladda SIE-filer (Sverige) till fact_balances i DuckDB.

SIE-formatet är YTD-baserat. För varje fil:
  #UB 0 <konto> <belopp>   → BS-konto, utgående balans (YTD)
  #RES 0 <konto> <belopp>  → IS-konto, resultatpost (YTD)
  #KONTO <konto> "<namn>"  → kontoplan (account_name lookup)
  #ORGNR <nr>              → bolagets orgnr (matchas mot dim_company)

Eftersom SIE-filer normalt täcker hela räkenskapsåret men endast har data
fram till genereringstidpunkten, härleds period från --period eller från
mappens namn (extracted/{YYYYMM}/Sweden/...). period_type sätts till 'ytd'.

Idempotens: rader för (company_id, period, source_kind, source_file) tas bort
innan nya skrivs.
"""
from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path

import duckdb

import db
from shared import begin_run, load_config, log, prev_month_period

SOURCE_KIND = "SIE"
PERIOD_TYPE = "ytd"
ENCODINGS = ("utf-8-sig", "cp437", "latin-1")

# SIE-rader vi bryr oss om
RE_ORGNR = re.compile(r"^#ORGNR\s+(\S+)", re.IGNORECASE)
RE_FNAMN = re.compile(r'^#FNAMN\s+"([^"]*)"', re.IGNORECASE)
RE_KONTO = re.compile(r'^#KONTO\s+(\S+)\s+"([^"]*)"', re.IGNORECASE)
RE_UB = re.compile(r"^#UB\s+0\s+(\S+)\s+(-?\d+(?:[.,]\d+)?)", re.IGNORECASE)
RE_RES = re.compile(r"^#RES\s+0\s+(\S+)\s+(-?\d+(?:[.,]\d+)?)", re.IGNORECASE)


def normalize_orgnr(orgnr: str) -> str:
    """Strip allt utom siffror — '556071-2340' → '5560712340'."""
    return re.sub(r"[^0-9]", "", str(orgnr).strip())


def read_text_with_fallback(path: Path) -> str:
    """Läs SIE-fil med encoding-fallback (samma kedja som process_sweden.py)."""
    last_err: Exception | None = None
    for enc in ENCODINGS:
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError as e:
            last_err = e
    raise UnicodeDecodeError(
        "sie", b"", 0, 0,
        f"Kunde inte läsa {path.name} med någon av {ENCODINGS}: {last_err}",
    )


def parse_sie(text: str) -> dict:
    """Returnera {orgnr, fnamn, konto: {code: name}, ub: [(code, amt)], res: [(code, amt)]}."""
    out = {"orgnr": None, "fnamn": None, "konto": {}, "ub": [], "res": []}
    for raw in text.splitlines():
        line = raw.lstrip()
        if not line or not line.startswith("#"):
            continue
        if m := RE_ORGNR.match(line):
            out["orgnr"] = m.group(1).strip('"')
        elif m := RE_FNAMN.match(line):
            out["fnamn"] = m.group(1)
        elif m := RE_KONTO.match(line):
            out["konto"][m.group(1)] = m.group(2)
        elif m := RE_UB.match(line):
            try:
                out["ub"].append((m.group(1), float(m.group(2).replace(",", "."))))
            except ValueError:
                continue
        elif m := RE_RES.match(line):
            try:
                out["res"].append((m.group(1), float(m.group(2).replace(",", "."))))
            except ValueError:
                continue
    return out


def build_orgnr_lookup(con: duckdb.DuckDBPyConnection) -> dict[str, tuple[int, str]]:
    """orgnr_normalized → (company_id, name) för alla bolag med orgnr.

    SIE är ett svenskt format så valutan är alltid SEK; vi tar ingen valuta
    från dim_company här (vissa CENTR/CA-bolag har svenskt orgnr men annan
    klassad valuta).
    """
    lookup: dict[str, tuple[int, str]] = {}
    for row in con.execute(
        "SELECT company_id, name, orgnr FROM dim_company "
        "WHERE orgnr IS NOT NULL AND orgnr <> ''"
    ).fetchall():
        cid, name, orgnr = row
        key = normalize_orgnr(orgnr)
        if key:
            lookup[key] = (cid, name)
    return lookup


def load_file(con, path: Path, base_path: Path, period: str,
              orgnr_lookup: dict, *, dry_run: bool) -> str:
    """Load one SIE file. Returns ok|warn|skip|error."""
    try:
        text = read_text_with_fallback(path)
    except Exception as e:
        log("ERROR", path.name, f"Läsfel: {e}")
        return "error"

    parsed = parse_sie(text)
    orgnr_raw = parsed.get("orgnr")
    if not orgnr_raw:
        log("ERROR", path.name, "Saknar #ORGNR")
        return "error"

    key = normalize_orgnr(orgnr_raw)
    hit = orgnr_lookup.get(key)
    if not hit:
        log("ERROR", path.name, f"OrgNr {orgnr_raw} saknas i dim_company")
        return "error"
    company_id, _name = hit
    currency = "SEK"  # SIE är svenskt format

    konto = parsed["konto"]
    rows: list[tuple] = []
    idx = 0
    for code, amt in parsed["ub"]:
        idx += 1
        rows.append((code, konto.get(code), amt, "BS", idx))
    for code, amt in parsed["res"]:
        idx += 1
        # SIE: #RES för IS-konton är ackumulerat resultat. Tecknet är som det
        # står i filen (intäkter typiskt negativa, kostnader positiva enligt
        # bokföringskonvention).
        rows.append((code, konto.get(code), amt, "IS", idx))

    if not rows:
        log("WARN", company_id, f"Inga UB/RES-rader i {path.name}")
        return "warn"

    total_ub = sum(r[2] for r in rows if r[3] == "BS")
    total_res = sum(r[2] for r in rows if r[3] == "IS")
    total = total_ub + total_res
    is_warn = abs(total) >= 1.0
    rel_src = db.relpath_from_base(path, base_path)
    now = datetime.now()

    if dry_run:
        log("INFO", company_id,
            f"[DRY] {path.name}  UB={len([r for r in rows if r[3]=='BS'])} "
            f"RES={len([r for r in rows if r[3]=='IS'])} "
            f"sum_ub={total_ub:.2f} sum_res={total_res:.2f} sum_tot={total:.2f}")
        return "warn" if is_warn else "ok"

    db.sync_dim_period(con, [period])

    con.execute("BEGIN")
    try:
        # Bara EN SIE-laddning per (bolag, period) — senaste filen vinner.
        # Två SIE-filer för samma bolag/period förekommer (olika export-versioner).
        con.execute(
            """DELETE FROM fact_balances
               WHERE company_id = ? AND period = ? AND source_kind = ?""",
            [company_id, period, SOURCE_KIND],
        )
        con.executemany(
            """INSERT INTO fact_balances
               (company_id, period, period_type, account_code, account_name,
                amount, currency, statement_type, source_kind, source_file,
                row_index, loaded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(company_id, period, PERIOD_TYPE, r[0], r[1], r[2], currency,
              r[3], SOURCE_KIND, rel_src, r[4], now) for r in rows],
        )
        con.execute(
            """INSERT INTO load_history
               (company_id, period, source_kind, source_file, rows_loaded,
                sum_amount, statement_type_present, status, message, loaded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [company_id, period, SOURCE_KIND, rel_src, len(rows), total, True,
             "warn" if is_warn else "ok",
             f"sum_ub={total_ub:.2f} sum_res={total_res:.2f}",
             now],
        )
        con.execute("COMMIT")
    except Exception as e:
        con.execute("ROLLBACK")
        log("ERROR", company_id, f"DB-fel {path.name}: {e}")
        return "error"

    status = "WARN" if is_warn else "OK"
    log(status, company_id, f"{path.name}  rader={len(rows)} sum={total:.2f}")
    return "warn" if is_warn else "ok"


def discover_files(source_dir: Path) -> list[Path]:
    """Hitta SIE-filer direkt i source_dir (inte i Referens/)."""
    if not source_dir.exists():
        return []
    return sorted(f for f in source_dir.iterdir()
                  if f.is_file() and f.suffix.upper() == ".SE")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ladda SIE-filer (Sverige) till DuckDB (fact_balances)."
    )
    parser.add_argument("--period", default=None,
                        help="YYYYMM. Default = föregående månad.")
    parser.add_argument("--source-dir", default=None,
                        help="Mapp att läsa från (default: extracted/{period}/Sweden under base_path)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    period = args.period or prev_month_period()
    begin_run("load_sie.py", period)
    log("START", "load_sie.py", f"period={period} dry_run={args.dry_run}")

    cfg = load_config()
    base_path = Path(cfg["base_path"])
    source_dir = Path(args.source_dir) if args.source_dir else \
        base_path / "extracted" / period / "Sweden"
    log("INFO", "scan", f"Söker SIE i {source_dir}")

    files = discover_files(source_dir)
    if not files:
        log("WARN", "scan", f"Inga .SE-filer hittades i {source_dir}")
        log("DONE", "load_sie.py", "0 OK  0 WARN  0 SKIP  0 ERROR")
        return

    con = db.connect()
    try:
        db.init_schema(con)
        orgnr_lookup = build_orgnr_lookup(con)
        if not orgnr_lookup:
            log("ERROR", "scan", "Ingen Sweden-bolag har orgnr i dim_company — kör 'py db.py' först")
            return
        counts = {"ok": 0, "warn": 0, "skip": 0, "error": 0}
        for f in files:
            status = load_file(con, f, base_path, period, orgnr_lookup,
                               dry_run=args.dry_run)
            counts[status] = counts.get(status, 0) + 1
    finally:
        con.close()

    log("DONE", "load_sie.py",
        f"{counts['ok']} OK  {counts['warn']} WARN  {counts['skip']} SKIP  {counts['error']} ERROR")


if __name__ == "__main__":
    main()
