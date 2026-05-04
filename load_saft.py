"""Ladda SAF-T-filer (Norge) till fact_balances i DuckDB.

SAF-T är YTD-baserat. För varje Account-element i MasterFiles:
  AccountID            → account_code
  AccountDescription   → account_name
  ClosingDebitBalance  / ClosingCreditBalance  → amount (debit - credit)
  AccountID-prefix     → statement_type (1/2 = BS, 3–9 = IS)

Period härleds från Header/SelectionCriteria (PeriodEndYear + PeriodEnd).
Bolag matchas via Header/Company/RegistrationNumber mot dim_company.orgnr.

Filer kan vara stora (20–50 MB); använder iterparse och stoppar efter
MasterFiles så att GeneralLedgerEntries (verifikat) inte läses in.

Idempotens: rader för (company_id, period, source_kind) tas bort innan nya
skrivs — flera SAF-T-filer för samma bolag/period överskriver varandra.
"""
from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import duckdb

import db
from shared import begin_run, load_config, log, prev_month_period

NS = "urn:StandardAuditFile-Taxation-Financial:NO"
SOURCE_KIND = "SAFT"
PERIOD_TYPE = "ytd"


def _t(elem: ET.Element, tag: str) -> str | None:
    """Hämta text från ett namespacad child-element, eller None."""
    found = elem.find(f"{{{NS}}}{tag}")
    return found.text if found is not None else None


def _amount(s: str | None) -> float:
    if not s:
        return 0.0
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return 0.0


def normalize_orgnr(orgnr: str) -> str:
    """Strip allt utom siffror.

    Hanterar svenska '556071-2340', norska '916059701', och norska
    moms-format som 'NO818488262MVA' eller '920595359MVA'.
    """
    import re
    return re.sub(r"[^0-9]", "", str(orgnr).strip())


def statement_type_from_code(account_code: str) -> str | None:
    """Norsk standard kontoplan: 1, 2 = BS; 3–9 = IS."""
    c = (account_code or "").strip()
    if not c or not c[0].isdigit():
        return None
    return "BS" if c[0] in ("1", "2") else "IS"


def parse_saft(path: Path) -> dict:
    """Returnera {orgnr, name, currency, period_start_year/month, period_end_year/month, accounts}.

    accounts: list of (account_code, account_name, amount, statement_type, row_index)
    """
    out: dict = {
        "orgnr": None, "name": None, "currency": None,
        "period_start_year": None, "period_start_month": None,
        "period_end_year": None, "period_end_month": None,
        "accounts": [],
    }
    accounts: list[tuple] = []
    idx = 0

    ctx = ET.iterparse(str(path), events=("end",))
    for event, elem in ctx:
        tag = elem.tag.split("}", 1)[-1] if "}" in elem.tag else elem.tag

        if tag == "Header":
            company = elem.find(f"{{{NS}}}Company")
            if company is not None:
                out["orgnr"] = _t(company, "RegistrationNumber")
                out["name"] = _t(company, "Name")
            out["currency"] = _t(elem, "DefaultCurrencyCode")
            sc = elem.find(f"{{{NS}}}SelectionCriteria")
            if sc is not None:
                out["period_start_month"] = _t(sc, "PeriodStart")
                out["period_start_year"] = _t(sc, "PeriodStartYear")
                out["period_end_month"] = _t(sc, "PeriodEnd")
                out["period_end_year"] = _t(sc, "PeriodEndYear")
            elem.clear()

        elif tag == "Account":
            code = _t(elem, "AccountID")
            name = _t(elem, "AccountDescription")
            cdb = _amount(_t(elem, "ClosingDebitBalance"))
            ccb = _amount(_t(elem, "ClosingCreditBalance"))
            amt = cdb - ccb
            st = statement_type_from_code(code) if code else None
            idx += 1
            accounts.append((code, name, amt, st, idx))
            elem.clear()

        elif tag == "GeneralLedgerEntries":
            # Vi är klara med MasterFiles — sluta läs (sparar minne + tid)
            elem.clear()
            break

    out["accounts"] = accounts
    return out


def derive_period(parsed: dict, override: str | None) -> str | None:
    """YYYYMM från SelectionCriteria.PeriodEndYear + PeriodEnd, eller override."""
    if override:
        return override
    y = parsed.get("period_end_year")
    m = parsed.get("period_end_month")
    if y and m:
        try:
            return f"{int(y):04d}{int(m):02d}"
        except ValueError:
            return None
    return None


def build_orgnr_lookup(con: duckdb.DuckDBPyConnection) -> dict[str, tuple[int, str]]:
    """orgnr_normalized → (company_id, name) för alla bolag med orgnr."""
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


def load_file(con, path: Path, base_path: Path, period_override: str | None,
              orgnr_lookup: dict, *, dry_run: bool) -> str:
    try:
        parsed = parse_saft(path)
    except ET.ParseError as e:
        log("ERROR", path.name, f"XML-parse-fel: {e}")
        return "error"
    except Exception as e:
        log("ERROR", path.name, f"Läsfel: {e}")
        return "error"

    orgnr_raw = parsed.get("orgnr")
    if not orgnr_raw:
        log("ERROR", path.name, "Saknar Header/Company/RegistrationNumber")
        return "error"

    hit = orgnr_lookup.get(normalize_orgnr(orgnr_raw))
    if not hit:
        log("ERROR", path.name, f"OrgNr {orgnr_raw} saknas i dim_company")
        return "error"
    company_id, _name = hit

    period = derive_period(parsed, period_override)
    if not period:
        log("ERROR", company_id, f"Kunde inte härleda period från {path.name}")
        return "error"

    rows = parsed["accounts"]
    if not rows:
        log("WARN", company_id, f"Inga Account-rader i {path.name}")
        return "warn"

    currency = parsed.get("currency") or "NOK"
    total_bs = sum(r[2] for r in rows if r[3] == "BS")
    total_is = sum(r[2] for r in rows if r[3] == "IS")
    total = total_bs + total_is
    is_warn = abs(total) >= 1.0
    rel_src = db.relpath_from_base(path, base_path)
    now = datetime.now()

    if dry_run:
        log("INFO", company_id,
            f"[DRY] {path.name}  period={period} BS={len([r for r in rows if r[3]=='BS'])} "
            f"IS={len([r for r in rows if r[3]=='IS'])} "
            f"sum_bs={total_bs:.2f} sum_is={total_is:.2f} sum_tot={total:.2f}")
        return "warn" if is_warn else "ok"

    db.sync_dim_period(con, [period])

    con.execute("BEGIN")
    try:
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
             f"sum_bs={total_bs:.2f} sum_is={total_is:.2f}", now],
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
    """Hitta SAF-T XML-filer direkt i source_dir (inte i Referens/)."""
    if not source_dir.exists():
        return []
    return sorted(f for f in source_dir.iterdir()
                  if f.is_file() and f.suffix.lower() == ".xml")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ladda SAF-T XML (Norge) till DuckDB (fact_balances)."
    )
    parser.add_argument("--period", default=None,
                        help="YYYYMM. Override för period (default: härleds från XML-Header)")
    parser.add_argument("--source-dir", default=None,
                        help="Mapp att läsa från (default: extracted/{period}/Norway under base_path)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    period_for_log = args.period or prev_month_period()
    begin_run("load_saft.py", period_for_log)
    log("START", "load_saft.py", f"period={args.period or '(auto)'} dry_run={args.dry_run}")

    cfg = load_config()
    base_path = Path(cfg["base_path"])
    source_dir = Path(args.source_dir) if args.source_dir else \
        base_path / "extracted" / period_for_log / "Norway"
    log("INFO", "scan", f"Söker SAF-T i {source_dir}")

    files = discover_files(source_dir)
    if not files:
        log("WARN", "scan", f"Inga .xml-filer hittades i {source_dir}")
        log("DONE", "load_saft.py", "0 OK  0 WARN  0 SKIP  0 ERROR")
        return

    con = db.connect()
    try:
        db.init_schema(con)
        orgnr_lookup = build_orgnr_lookup(con)
        if not orgnr_lookup:
            log("ERROR", "scan", "Inga bolag med orgnr i dim_company — kör 'py db.py' först")
            return
        counts = {"ok": 0, "warn": 0, "skip": 0, "error": 0}
        for f in files:
            status = load_file(con, f, base_path, args.period, orgnr_lookup,
                               dry_run=args.dry_run)
            counts[status] = counts.get(status, 0) + 1
    finally:
        con.close()

    log("DONE", "load_saft.py",
        f"{counts['ok']} OK  {counts['warn']} WARN  {counts['skip']} SKIP  {counts['error']} ERROR")


if __name__ == "__main__":
    main()
