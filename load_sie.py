"""Ladda SIE-filer (Sverige) till fact_balances i DuckDB.

SIE-formatet är YTD-baserat. För varje fil:
  #UB 0 <konto> <belopp>            → BS-konto, utgående balans (YTD)
  #RES 0 <konto> <belopp>           → IS-konto, ackumulerat resultat (YTD)
  #PSALDO 0 <YYYYMM> <konto> {} <belopp>
                                    → per-månad YTD-saldo per konto.
                                      Ger månadsvis YTD-snapshot även från
                                      en enda senaste-månad-fil.
  #KONTO <konto> "<namn>"           → kontoplan
  #ORGNR / #FNAMN / #RAR / #GEN     → metadata

Period-härledning (#PSALDO är det enda fält i SIE som faktiskt anger
"data fram till och med"; #GEN är exportdatum, #RAR är räkenskapsårets slut):
  - --period given:
      OK om filen saknar #PSALDO eller om max(#PSALDO) == --period.
      ERROR om max(#PSALDO) != --period (skydd mot felklassning).
  - --period inte given:
      OK om filen har #PSALDO → använd max.
      ERROR annars (kräv explicit --period).

Två separata source_kind-laner skrivs till fact_balances:
  SOURCE_KIND='SIE'         → UB/RES, period=fastställd för filen
  SOURCE_KIND='SIE_PSALDO'  → PSALDO, period=PSALDO-radens egna YYYYMM

Idempotens: senaste laddningen vinner per (company_id, period, source_kind).
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
SOURCE_KIND_PSALDO = "SIE_PSALDO"
PERIOD_TYPE = "ytd"
ENCODINGS = ("utf-8-sig", "cp437", "latin-1")

RE_ORGNR  = re.compile(r"^#ORGNR\s+(\S+)", re.IGNORECASE)
RE_FNAMN  = re.compile(r'^#FNAMN\s+"([^"]*)"', re.IGNORECASE)
RE_KONTO  = re.compile(r'^#KONTO\s+(\S+)\s+"([^"]*)"', re.IGNORECASE)
RE_UB     = re.compile(r"^#UB\s+0\s+(\S+)\s+(-?\d+(?:[.,]\d+)?)", re.IGNORECASE)
RE_RES    = re.compile(r"^#RES\s+0\s+(\S+)\s+(-?\d+(?:[.,]\d+)?)", re.IGNORECASE)
RE_PSALDO = re.compile(
    r"^#PSALDO\s+0\s+(\d{6})\s+(\S+)\s+\{[^}]*\}\s+(-?\d+(?:[.,]\d+)?)",
    re.IGNORECASE,
)
RE_RAR0   = re.compile(r"^#RAR\s+0\s+(\d{8})\s+(\d{8})", re.IGNORECASE)
RE_GEN    = re.compile(r"^#GEN\s+(\d{8})", re.IGNORECASE)
RE_VER    = re.compile(
    r'^#VER\s+(\S+)\s+(\S+)\s+(\d{8})'
    r'(?:\s+"([^"]*)")?',
    re.IGNORECASE,
)
RE_TRANS  = re.compile(
    r'^#TRANS\s+(\S+)\s+\{[^}]*\}\s+(-?\d+(?:[.,]\d+)?)'  # konto, dim, belopp
    r'(?:\s+"([^"]*)")?'                                    # transdate (str)
    r'(?:\s+"([^"]*)")?'                                    # text
    r'(?:\s+(-?\d+(?:[.,]\d+)?))?',                         # quantity
    re.IGNORECASE,
)
JOURNAL_BATCH = 5000


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


def parse_sie(text: str, *, with_journal: bool = False) -> dict:
    """Returnera parsed SIE-data.

    Saldonycklar: orgnr, fnamn, konto{code:name}, ub[(code,amt)],
    res[(code,amt)], psaldo[(period,code,amt)], rar_start, rar_end, gen_date.

    Med with_journal=True även: vouchers[{series,number,date,text,transes[
        {line_no,account,amount,trans_text,quantity}]}].
    """
    out: dict = {
        "orgnr": None, "fnamn": None, "konto": {},
        "ub": [], "res": [], "psaldo": [],
        "rar_start": None, "rar_end": None, "gen_date": None,
        "vouchers": [],
    }
    current_voucher = None
    in_block = False
    line_no_in_voucher = 0

    for raw in text.splitlines():
        line = raw.lstrip()
        if not line:
            continue
        # Block-delimiterare: { öppnar TRANS-blocket för senast lästa #VER, } stänger.
        if line[0] == "{":
            in_block = True
            line_no_in_voucher = 0
            continue
        if line[0] == "}":
            in_block = False
            current_voucher = None
            continue
        if not line.startswith("#"):
            continue

        if in_block:
            if with_journal and current_voucher is not None and (m := RE_TRANS.match(line)):
                try:
                    amt = float(m.group(2).replace(",", "."))
                except ValueError:
                    continue
                line_no_in_voucher += 1
                quantity = None
                if m.group(5):
                    try:
                        quantity = float(m.group(5).replace(",", "."))
                    except ValueError:
                        quantity = None
                current_voucher["transes"].append({
                    "line_no": line_no_in_voucher,
                    "account": m.group(1),
                    "amount": amt,
                    "trans_text": m.group(4),
                    "quantity": quantity,
                })
            continue

        # Top-level (inte i block)
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
        elif m := RE_PSALDO.match(line):
            try:
                out["psaldo"].append((
                    m.group(1), m.group(2),
                    float(m.group(3).replace(",", ".")),
                ))
            except ValueError:
                continue
        elif m := RE_RAR0.match(line):
            out["rar_start"] = m.group(1)
            out["rar_end"] = m.group(2)
        elif m := RE_GEN.match(line):
            out["gen_date"] = m.group(1)
        elif with_journal and (m := RE_VER.match(line)):
            current_voucher = {
                "series": m.group(1).strip('"'),
                "number": m.group(2).strip('"'),
                "date": m.group(3),
                "text": m.group(4),
                "transes": [],
            }
            out["vouchers"].append(current_voucher)
    return out


def vouchers_to_journal_rows(parsed: dict, company_id: int, currency: str,
                             rel_src: str, now: datetime) -> tuple[list[tuple], set[str]]:
    """Plana ut vouchers → rader för fact_journal_sie. Returnerar (rows, periods)."""
    konto = parsed["konto"]
    rows: list[tuple] = []
    periods: set[str] = set()
    for v in parsed["vouchers"]:
        d = v["date"]  # 'YYYYMMDD'
        period = d[:6]
        periods.add(period)
        try:
            from datetime import date as _date
            voucher_date = _date(int(d[:4]), int(d[4:6]), int(d[6:8]))
        except (ValueError, IndexError):
            continue
        for t in v["transes"]:
            rows.append((
                company_id, period, v["series"], v["number"],
                voucher_date, v["text"], t["line_no"],
                t["account"], konto.get(t["account"]),
                t["amount"], t["trans_text"], t["quantity"],
                currency, rel_src, now,
            ))
    return rows, periods


def derive_period(parsed: dict) -> str | None:
    """Endast #PSALDO är ett tillförlitligt 'data-through'-signal i SIE.

    #GEN är exportdatum (kan vara senare än datat) och #RAR är FY-slut
    (alltid YYYY1231 för månadsfiler). Båda är därför värdelösa för att
    avgöra vilken period datat representerar.
    """
    if parsed["psaldo"]:
        return max(p for p, _, _ in parsed["psaldo"])
    return None


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


def load_file(con, path: Path, base_path: Path, period_override: str | None,
              orgnr_lookup: dict, *, dry_run: bool, include_journal: bool = False) -> str:
    """Load one SIE file. Returns ok|warn|skip|error."""
    try:
        text = read_text_with_fallback(path)
    except Exception as e:
        log("ERROR", path.name, f"Läsfel: {e}")
        return "error"

    parsed = parse_sie(text, with_journal=include_journal)
    orgnr_raw = parsed.get("orgnr")
    if not orgnr_raw:
        log("ERROR", path.name, "Saknar #ORGNR")
        return "error"

    hit = orgnr_lookup.get(normalize_orgnr(orgnr_raw))
    if not hit:
        log("ERROR", path.name, f"OrgNr {orgnr_raw} saknas i dim_company")
        return "error"
    company_id, _name = hit
    currency = "SEK"

    period_derived = derive_period(parsed)  # max #PSALDO eller None
    if period_override:
        period = period_override
        if period_derived and period_derived != period:
            log("ERROR", company_id,
                f"Period-mismatch i {path.name}: --period={period_override} "
                f"men max #PSALDO={period_derived}. "
                "Skydd mot felklassning av YTD-data.")
            return "error"
    elif period_derived:
        period = period_derived
    else:
        log("ERROR", company_id,
            f"{path.name} saknar #PSALDO — kan inte avgöra data-through. "
            "Ange --period YYYYMM explicit.")
        return "error"

    konto = parsed["konto"]

    # IS/BS-klassning per kontokod: UB → BS, RES → IS. Fallback för PSALDO-koder
    # som ev. saknas i UB/RES: första-siffra-regel (1,2 → BS; annars IS).
    code_st: dict[str, str] = {}
    for code, _ in parsed["ub"]:
        code_st[code] = "BS"
    for code, _ in parsed["res"]:
        code_st[code] = "IS"

    def st_for(code: str) -> str | None:
        if code in code_st:
            return code_st[code]
        c = (code or "").strip()
        if not c or not c[0].isdigit():
            return None
        return "BS" if c[0] in ("1", "2") else "IS"

    sie_rows: list[tuple] = []
    idx = 0
    for code, amt in parsed["ub"]:
        idx += 1
        sie_rows.append((code, konto.get(code), amt, "BS", idx))
    for code, amt in parsed["res"]:
        idx += 1
        sie_rows.append((code, konto.get(code), amt, "IS", idx))

    # PSALDO-rader: en lane per fil; period kommer från radens egen YYYYMM.
    psaldo_rows: list[tuple] = []
    idx_per_period: dict[str, int] = {}
    for p, code, amt in parsed["psaldo"]:
        idx_per_period[p] = idx_per_period.get(p, 0) + 1
        psaldo_rows.append(
            (p, code, konto.get(code), amt, st_for(code), idx_per_period[p])
        )
    psaldo_periods = sorted({r[0] for r in psaldo_rows})

    if not sie_rows and not psaldo_rows:
        log("WARN", company_id, f"Inga UB/RES/PSALDO-rader i {path.name}")
        return "warn"

    total_ub = sum(r[2] for r in sie_rows if r[3] == "BS")
    total_res = sum(r[2] for r in sie_rows if r[3] == "IS")
    total = total_ub + total_res
    is_warn = abs(total) >= 1.0
    rel_src = db.relpath_from_base(path, base_path)
    now = datetime.now()

    journal_rows: list[tuple] = []
    journal_periods: set[str] = set()
    if include_journal and parsed["vouchers"]:
        journal_rows, journal_periods = vouchers_to_journal_rows(
            parsed, company_id, currency, rel_src, now,
        )

    if dry_run:
        journal_msg = (f" JOURNAL={len(journal_rows)} ({len(journal_periods)} mån)"
                       if include_journal else "")
        log("INFO", company_id,
            f"[DRY] {path.name}  period={period} "
            f"UB={len([r for r in sie_rows if r[3]=='BS'])} "
            f"RES={len([r for r in sie_rows if r[3]=='IS'])} "
            f"PSALDO={len(psaldo_rows)} ({len(psaldo_periods)} mån)"
            f"{journal_msg} "
            f"sum_ub={total_ub:.2f} sum_res={total_res:.2f} sum_tot={total:.2f}")
        return "warn" if is_warn else "ok"

    db.sync_dim_period(con, [period] + psaldo_periods + sorted(journal_periods))

    con.execute("BEGIN")
    try:
        # SIE (UB/RES): senaste laddningen vinner per (bolag, period).
        con.execute(
            """DELETE FROM fact_balances
               WHERE company_id = ? AND period = ? AND source_kind = ?""",
            [company_id, period, SOURCE_KIND],
        )
        if sie_rows:
            con.executemany(
                """INSERT INTO fact_balances
                   (company_id, period, period_type, account_code, account_name,
                    amount, currency, statement_type, source_kind, source_file,
                    row_index, loaded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(company_id, period, PERIOD_TYPE, r[0], r[1], r[2], currency,
                  r[3], SOURCE_KIND, rel_src, r[4], now) for r in sie_rows],
            )

        # PSALDO: senaste laddningen vinner per (bolag, period). Mars-filens
        # PSALDO för 202601 ersätter en ev. tidigare 202601-laddning från
        # samma eller annan SIE-fil.
        if psaldo_periods:
            placeholders = ",".join("?" * len(psaldo_periods))
            con.execute(
                f"""DELETE FROM fact_balances
                    WHERE company_id = ? AND source_kind = ?
                    AND period IN ({placeholders})""",
                [company_id, SOURCE_KIND_PSALDO, *psaldo_periods],
            )
            con.executemany(
                """INSERT INTO fact_balances
                   (company_id, period, period_type, account_code, account_name,
                    amount, currency, statement_type, source_kind, source_file,
                    row_index, loaded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(company_id, r[0], PERIOD_TYPE, r[1], r[2], r[3], currency,
                  r[4], SOURCE_KIND_PSALDO, rel_src, r[5], now)
                 for r in psaldo_rows],
            )

        # Journal: senaste laddningen vinner per (bolag, period). En SIE-fil
        # täcker hela YTD så vouchers för 202601 från en mars-fil ersätter
        # ev. tidigare 202601-laddning från en annan SIE.
        if journal_periods:
            jp_sorted = sorted(journal_periods)
            placeholders = ",".join("?" * len(jp_sorted))
            con.execute(
                f"""DELETE FROM fact_journal_sie
                    WHERE company_id = ? AND period IN ({placeholders})""",
                [company_id, *jp_sorted],
            )
            for i in range(0, len(journal_rows), JOURNAL_BATCH):
                con.executemany(
                    """INSERT INTO fact_journal_sie
                       (company_id, period, series, voucher_number, voucher_date,
                        voucher_text, line_no, account_code, account_name,
                        amount, transaction_text, quantity, currency,
                        source_file, loaded_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    journal_rows[i:i + JOURNAL_BATCH],
                )

        con.execute(
            """INSERT INTO load_history
               (company_id, period, source_kind, source_file, rows_loaded,
                sum_amount, statement_type_present, status, message, loaded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [company_id, period, SOURCE_KIND, rel_src,
             len(sie_rows) + len(psaldo_rows) + len(journal_rows), total, True,
             "warn" if is_warn else "ok",
             f"sie_rows={len(sie_rows)} psaldo_rows={len(psaldo_rows)} "
             f"psaldo_periods={len(psaldo_periods)} "
             f"journal_rows={len(journal_rows)} journal_periods={len(journal_periods)} "
             f"sum_ub={total_ub:.2f} sum_res={total_res:.2f}",
             now],
        )
        con.execute("COMMIT")
    except Exception as e:
        con.execute("ROLLBACK")
        log("ERROR", company_id, f"DB-fel {path.name}: {e}")
        return "error"

    status = "WARN" if is_warn else "OK"
    psaldo_msg = f" PSALDO={len(psaldo_rows)}({len(psaldo_periods)} mån)" if psaldo_rows else ""
    journal_msg = f" JOURNAL={len(journal_rows)}({len(journal_periods)} mån)" if journal_rows else ""
    log(status, company_id,
        f"{path.name}  period={period}  rader={len(sie_rows)}{psaldo_msg}{journal_msg}  sum={total:.2f}")
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
                        help="YYYYMM. Override för period-validering "
                             "(default: härleds från filen)")
    parser.add_argument("--source-dir", default=None,
                        help="Mapp att läsa från "
                             "(default: extracted/{period}/Sweden under base_path)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--include-journal", action="store_true",
                        help="Ladda även #VER/#TRANS till fact_journal_sie "
                             "(opt-in, kan vara tungt för stora filer)")
    args = parser.parse_args()

    period_for_log = args.period or prev_month_period()
    begin_run("load_sie.py", period_for_log)
    log("START", "load_sie.py",
        f"period={args.period or '(auto)'} dry_run={args.dry_run} "
        f"journal={args.include_journal}")

    cfg = load_config()
    base_path = Path(cfg["base_path"])
    source_dir = Path(args.source_dir) if args.source_dir else \
        base_path / "extracted" / period_for_log / "Sweden"
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
            log("ERROR", "scan",
                "Inga bolag med orgnr i dim_company — kör 'py db.py' först")
            return
        counts = {"ok": 0, "warn": 0, "skip": 0, "error": 0}
        for f in files:
            status = load_file(con, f, base_path, args.period, orgnr_lookup,
                               dry_run=args.dry_run,
                               include_journal=args.include_journal)
            counts[status] = counts.get(status, 0) + 1
    finally:
        con.close()

    log("DONE", "load_sie.py",
        f"{counts['ok']} OK  {counts['warn']} WARN  "
        f"{counts['skip']} SKIP  {counts['error']} ERROR")


if __name__ == "__main__":
    main()
