"""Ladda SAF-T-filer (Norge + Danmark) till fact_balances i Postgres.

SAF-T är YTD-baserat. För varje Account-element i MasterFiles:
  AccountID            → account_code
  AccountDescription   → account_name
  ClosingDebitBalance  / ClosingCreditBalance  → amount (debit - credit)
  AccountID-prefix     → statement_type (NO: 1/2=BS, 3-9=IS;
                                          DK: 4-siffrigt prefix ≤4999=IS, ≥5000=BS)

Namespace detekteras automatiskt från XML-roten:
  urn:StandardAuditFile-Taxation-Financial:NO  → NO (default-valuta NOK)
  urn:StandardAuditFile-Taxation-Financial:DK  → DK (default-valuta DKK)

Period härleds från Header/SelectionCriteria:
  - PeriodEndYear + PeriodEnd (NO, DK E-Komplet)
  - SelectionEndDate (DK Visma Business, ISO-datum)

Bolag matchas via Header/Company/RegistrationNumber mot dim_company.orgnr
(blanksteg/icke-siffror normaliseras bort — DK Visma skriver "29 14 36 25").

Filer kan vara stora (NO: 20–50 MB; DK Actas: 280+ MB); använder iterparse.
För balance-parsen stoppas läsningen vid GeneralLedgerEntries så att
verifikat (separat iter) inte läses in en gång till.

Idempotens: rader för (company_id, period, source_kind) tas bort innan nya
skrivs — flera SAF-T-filer för samma bolag/period överskriver varandra.
"""
from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import db
from shared import begin_run, is_override_for, load_config, log, prev_month_period

NS_BY_COUNTRY = {
    "NO": "urn:StandardAuditFile-Taxation-Financial:NO",
    "DK": "urn:StandardAuditFile-Taxation-Financial:DK",
}
NS_TO_COUNTRY = {v: k for k, v in NS_BY_COUNTRY.items()}
DEFAULT_CURRENCY = {"NO": "NOK", "DK": "DKK"}

SOURCE_KIND = "SAFT"
PERIOD_TYPE = "ytd"
JOURNAL_BATCH = 5000

# Underkataloger (under extracted/{period}/) som scannas för SAF-T-XML
COUNTRY_DIRS = {"NO": "Norway", "DK": "Denmark"}

# SAF-T-filer som saknar RegistrationNumber i Header: filnamnssubsträng → company_id
# (Säkerhetsnät — normalt löses Actas via orgnr-lookup på "29 14 36 25".)
FILENAME_OVERRIDES: dict[str, int] = {
    "081_Actas": 81,  # Actas DK
}


def _detect_namespace(path: Path) -> str | None:
    """Läs första elementet och returnera dess namespace-URI ('' om saknas)."""
    for event, elem in ET.iterparse(str(path), events=("start",)):
        if "}" in elem.tag:
            return elem.tag.split("}", 1)[0][1:]
        return ""
    return None


def _t(elem: ET.Element, tag: str, ns: str) -> str | None:
    """Hämta text från ett namespacad child-element, eller None."""
    found = elem.find(f"{{{ns}}}{tag}")
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

    Hanterar svenska '556071-2340', norska '916059701', norska
    moms-format som 'NO818488262MVA' / '920595359MVA', och danska
    Visma-formatet med blanksteg '29 14 36 25'.
    """
    import re
    return re.sub(r"[^0-9]", "", str(orgnr).strip())


def statement_type_from_code(account_code: str, country: str) -> str | None:
    """Kontotyp → statement_type per land.

    NO: norsk standard kontoplan — 1, 2 = BS; 3–9 = IS.
    DK: 4-siffrigt prefix (Visma Business / dansk SKAT-konvention) —
        ≤ 4999 = IS, ≥ 5000 = BS. Längre kontonummer (5-6 siffror)
        klassificeras på sina första 4 siffror (motsvarar
        process_denmark.py:normalize4()).
    """
    c = (account_code or "").strip()
    if not c or not c[0].isdigit():
        return None
    if country == "DK":
        digits = "".join(ch for ch in c if ch.isdigit())
        try:
            prefix4 = int(digits[:4]) if len(digits) > 4 else int(digits)
        except ValueError:
            return None
        return "IS" if prefix4 <= 4999 else "BS"
    # NO (default)
    return "BS" if c[0] in ("1", "2") else "IS"


def parse_saft(path: Path) -> dict:
    """Returnera dict med metadata + accounts från SAF-T-filen.

    Nycklar:
      ns, country, orgnr, name, currency,
      period_start_year/month, period_end_year/month,
      selection_start_date, selection_end_date,
      accounts: list of (account_code, account_name, amount, statement_type, row_index)
    """
    ns = _detect_namespace(path) or ""
    country = NS_TO_COUNTRY.get(ns)

    out: dict = {
        "ns": ns, "country": country,
        "orgnr": None, "name": None, "currency": None,
        "period_start_year": None, "period_start_month": None,
        "period_end_year": None, "period_end_month": None,
        "selection_start_date": None, "selection_end_date": None,
        "accounts": [],
    }
    accounts: list[tuple] = []
    idx = 0

    ctx = ET.iterparse(str(path), events=("end",))
    for event, elem in ctx:
        tag = elem.tag.split("}", 1)[-1] if "}" in elem.tag else elem.tag

        if tag == "Header":
            company = elem.find(f"{{{ns}}}Company")
            if company is not None:
                out["orgnr"] = _t(company, "RegistrationNumber", ns)
                out["name"] = _t(company, "Name", ns)
            out["currency"] = _t(elem, "DefaultCurrencyCode", ns)
            sc = elem.find(f"{{{ns}}}SelectionCriteria")
            if sc is not None:
                out["period_start_month"] = _t(sc, "PeriodStart", ns)
                out["period_start_year"] = _t(sc, "PeriodStartYear", ns)
                out["period_end_month"] = _t(sc, "PeriodEnd", ns)
                out["period_end_year"] = _t(sc, "PeriodEndYear", ns)
                out["selection_start_date"] = _t(sc, "SelectionStartDate", ns)
                out["selection_end_date"] = _t(sc, "SelectionEndDate", ns)
            elem.clear()

        elif tag == "Account":
            code = _t(elem, "AccountID", ns)
            name = _t(elem, "AccountDescription", ns)
            cdb = _amount(_t(elem, "ClosingDebitBalance", ns))
            ccb = _amount(_t(elem, "ClosingCreditBalance", ns))
            amt = cdb - ccb
            st = statement_type_from_code(code, country) if code else None
            idx += 1
            accounts.append((code, name, amt, st, idx))
            elem.clear()

        elif tag == "GeneralLedgerEntries":
            # Vi är klara med MasterFiles — sluta läs (sparar minne + tid).
            # Journal-rader hämtas separat via iter_saft_journal().
            elem.clear()
            break

    out["accounts"] = accounts
    return out


def _parse_iso_date(s: str | None):
    """'2026-03-15' → date(2026,3,15), tomt/ogiltigt → None."""
    if not s:
        return None
    try:
        from datetime import date as _date
        return _date(int(s[:4]), int(s[5:7]), int(s[8:10]))
    except (ValueError, IndexError):
        return None


def iter_saft_journal(path: Path, ns: str | None = None):
    """Yield en dict per Line under GeneralLedgerEntries.

    Strömmande iterparse — clearar varje Journal efter bearbetning så att
    minnet hålls bundet till en Journal i taget. För monatliga norska
    SAF-T-filer rymms detta enkelt; för stora danska årsfiler (Actas
    ~300 MB) skalar det också.

    ns kan skickas in om den redan är känd, annars detekteras den från
    rotelementet.
    """
    if ns is None:
        ns = _detect_namespace(path) or ""

    ctx = ET.iterparse(str(path), events=("end",))
    in_gle = False
    for event, elem in ctx:
        tag = elem.tag.split("}", 1)[-1] if "}" in elem.tag else elem.tag

        if tag == "Header" or tag == "MasterFiles":
            elem.clear()
            continue

        if tag == "GeneralLedgerEntries":
            elem.clear()
            return  # klar

        if tag == "Journal":
            in_gle = True
            j_id = _t(elem, "JournalID", ns)
            j_desc = _t(elem, "Description", ns)
            for tx in elem.findall(f"{{{ns}}}Transaction"):
                tx_id = _t(tx, "TransactionID", ns)
                tx_date = _parse_iso_date(_t(tx, "TransactionDate", ns))
                tx_desc = _t(tx, "Description", ns)
                line_no = 0
                for line in tx.findall(f"{{{ns}}}Line"):
                    line_no += 1
                    rec_id = _t(line, "RecordID", ns)
                    acc = _t(line, "AccountID", ns)
                    line_desc = _t(line, "Description", ns)
                    debit_elem = line.find(f"{{{ns}}}DebitAmount")
                    credit_elem = line.find(f"{{{ns}}}CreditAmount")
                    debit = _amount(_t(debit_elem, "Amount", ns)) if debit_elem is not None else 0.0
                    credit = _amount(_t(credit_elem, "Amount", ns)) if credit_elem is not None else 0.0
                    yield {
                        "journal_id": j_id, "journal_desc": j_desc,
                        "transaction_id": tx_id, "transaction_date": tx_date,
                        "transaction_desc": tx_desc,
                        "line_no": line_no, "record_id": rec_id,
                        "account_code": acc, "line_desc": line_desc,
                        "debit": debit, "credit": credit,
                    }
            elem.clear()  # frigör hela Journal:n med dess Transactions/Lines

    if not in_gle:
        return


def _yyyymm_from_iso(date_str: str | None) -> str | None:
    """'2026-04-30' → '202604'. Returnerar None vid ogiltigt format."""
    if not date_str or len(date_str) < 7:
        return None
    try:
        return f"{int(date_str[:4]):04d}{int(date_str[5:7]):02d}"
    except ValueError:
        return None


def _journal_period(j: dict, fallback: str) -> str:
    """YYYYMM för en journal-rad — från transaction_date, annars fallback."""
    d = j["transaction_date"]
    return f"{d.year:04d}{d.month:02d}" if d else fallback


def derive_fy_range(parsed: dict, period: str) -> tuple[str, str]:
    """Räkenskapsårets (start_period, end_period) som 'YYYYMM'.

    Försök i tur och ordning:
      1. SelectionCriteria.PeriodStartYear+PeriodStart / PeriodEndYear+PeriodEnd
         (NO + DK E-Komplet)
      2. SelectionCriteria.SelectionStartDate / SelectionEndDate (DK Visma)
      3. Kalenderår från period:t självt (fallback)
    """
    sy = parsed.get("period_start_year")
    sm = parsed.get("period_start_month")
    ey = parsed.get("period_end_year")
    em = parsed.get("period_end_month")
    try:
        if sy and sm and ey and em:
            start = f"{int(sy):04d}{int(sm):02d}"
            end = f"{int(ey):04d}{int(em):02d}"
            return start, end
    except ValueError:
        pass

    start = _yyyymm_from_iso(parsed.get("selection_start_date"))
    end = _yyyymm_from_iso(parsed.get("selection_end_date"))
    if start and end:
        return start, end

    year = period[:4]
    return f"{year}01", f"{year}12"


def derive_period(parsed: dict, override: str | None) -> str | None:
    """YYYYMM via (i) --period override, (ii) PeriodEndYear+PeriodEnd,
    (iii) SelectionEndDate ISO. Sista chansen — annars None.
    """
    if override:
        return override
    y = parsed.get("period_end_year")
    m = parsed.get("period_end_month")
    if y and m:
        try:
            return f"{int(y):04d}{int(m):02d}"
        except ValueError:
            pass
    return _yyyymm_from_iso(parsed.get("selection_end_date"))


def build_orgnr_lookup(con: db.Conn) -> dict[str, tuple[int, str]]:
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
              orgnr_lookup: dict, *, dry_run: bool, include_journal: bool = False,
              override: list[int] | None = None) -> str:
    try:
        parsed = parse_saft(path)
    except ET.ParseError as e:
        log("ERROR", path.name, f"XML-parse-fel: {e}")
        return "error"
    except Exception as e:
        log("ERROR", path.name, f"Läsfel: {e}")
        return "error"

    country = parsed.get("country")
    if country not in NS_BY_COUNTRY:
        log("ERROR", path.name,
            f"Okänd/avsaknad SAF-T-namespace ({parsed.get('ns')!r}); "
            f"stödda: {list(NS_BY_COUNTRY)}")
        return "error"

    orgnr_raw = parsed.get("orgnr")
    company_id: int | None = None
    if not orgnr_raw:
        for substr, cid in FILENAME_OVERRIDES.items():
            if substr in path.name:
                company_id = cid
                log("INFO", path.name,
                    f"RegistrationNumber saknas — filename-override → company_id={cid}")
                break
        if company_id is None:
            log("ERROR", path.name,
                "Saknar Header/Company/RegistrationNumber (lägg till i FILENAME_OVERRIDES?)")
            return "error"
    else:
        hit = orgnr_lookup.get(normalize_orgnr(orgnr_raw))
        if not hit:
            log("ERROR", path.name, f"OrgNr {orgnr_raw} saknas i dim_company")
            return "error"
        company_id, _name = hit

    rows = parsed["accounts"]
    if not rows:
        # DK E-Komplet skickar ibland SAF-T-export utan GeneralLedgerAccounts
        # (bara Customers/Suppliers). Loggas som WARN och skippas — vi har
        # ingen balance att ladda och vill inte felklassa det som ERROR.
        log("WARN", company_id,
            f"{path.name}: inga GL-konton (country={country}). "
            "SAF-T-export saknar GeneralLedgerAccounts — skippar balance-load.")
        return "warn"

    period = derive_period(parsed, period_override)
    if not period:
        log("ERROR", company_id, f"Kunde inte härleda period från {path.name}")
        return "error"

    # Vakt mot fel-fil-i-fel-mapp: om --period är satt och filens egen
    # PeriodEnd är *tidigare* än override, är det garanterat fel fil (t.ex.
    # en januari-SAF-T som hamnat i extracted/202604/). Header *senare* än
    # override är OK — Tema Total m.fl. exporterar helårs-SAF-T med
    # PeriodEnd=YYYY12 även när bara YTD-data finns.
    if period_override:
        header_period = derive_period(parsed, None)
        if header_period and header_period < period_override:
            log("ERROR", company_id,
                f"{path.name}: filens PeriodEnd={header_period} < "
                f"--period={period_override}. Fel fil i fel mapp? Skippar.")
            return "error"

    # Konfliktkoll: finns redan SAFT för perioder >= filens period inom FY?
    fy_start, fy_end = derive_fy_range(parsed, period)
    has_override = is_override_for(override, company_id)
    existing = con.execute(
        """SELECT COUNT(*) FROM fact_balances
           WHERE company_id = %s AND source_kind = %s
             AND period >= %s AND period BETWEEN %s AND %s""",
        [company_id, SOURCE_KIND, period, fy_start, fy_end],
    ).fetchone()[0]
    if existing > 0 and not has_override:
        log("SKIP", company_id,
            f"{path.name}  SAFT redan inläst för period >= {period} "
            f"inom FY {fy_start}-{fy_end} ({existing} rader). "
            "Kör med --override för att skriva över.")
        return "skip"

    currency = parsed.get("currency") or DEFAULT_CURRENCY[country]
    total_bs = sum(r[2] for r in rows if r[3] == "BS")
    total_is = sum(r[2] for r in rows if r[3] == "IS")
    total = total_bs + total_is
    # SAF-T: BS+IS = årets resultat (YTD), inte 0. Saldobalans-check görs inte
    # här — använd verifikatnivå (fact_journal_saft) för debet/kredit-balans.
    rel_src = db.relpath_from_base(path, base_path)
    now = datetime.now()

    if dry_run:
        journal_msg = ""
        if include_journal:
            # Räkna i dry-run för synlighet (extra pass — endast vid --dry-run)
            try:
                jcount = sum(1 for _ in iter_saft_journal(path, parsed["ns"]))
                journal_msg = f"  JOURNAL≈{jcount}"
            except Exception as e:
                journal_msg = f"  JOURNAL=läsfel ({e})"
        ovr = f"  OVERRIDE (raderar {existing} rader inom FY)" if (existing > 0 and has_override) else ""
        log("OK", company_id,
            f"[DRY] {path.name}  period={period} FY={fy_start}-{fy_end} "
            f"BS={len([r for r in rows if r[3]=='BS'])} "
            f"IS={len([r for r in rows if r[3]=='IS'])} "
            f"sum_bs={total_bs:.2f} sum_is={total_is:.2f} sum_tot={total:.2f}{journal_msg}{ovr}")
        return "ok"

    if existing > 0 and has_override:
        log("INFO", company_id,
            f"OVERRIDE: skriver över {existing} SAFT-rader för "
            f"period >= {period} inom FY {fy_start}-{fy_end}")

    db.sync_dim_period(con, [period])

    con.execute("BEGIN")
    try:
        # Override: rensa SAFT (period > filens period inom FY — egna period
        # rensas av period-DELETE nedan) och journal HELA FY:t. Den period-
        # nyckade journal-DELETE nedan täcker bara perioderna i den nya filen;
        # vid override vill vi även rensa ev. stale journal i senare månader.
        if has_override and existing > 0:
            con.execute(
                """DELETE FROM fact_balances
                   WHERE company_id = %s AND source_kind = %s
                     AND period > %s AND period BETWEEN %s AND %s""",
                [company_id, SOURCE_KIND, period, fy_start, fy_end],
            )
            con.execute(
                """DELETE FROM fact_journal_saft
                   WHERE company_id = %s AND period BETWEEN %s AND %s""",
                [company_id, fy_start, fy_end],
            )
        con.execute(
            """DELETE FROM fact_balances
               WHERE company_id = %s AND period = %s AND source_kind = %s""",
            [company_id, period, SOURCE_KIND],
        )
        con.executemany(
            """INSERT INTO fact_balances
               (company_id, period, period_type, account_code, account_name,
                amount, currency, statement_type, source_kind, source_file,
                row_index, loaded_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [(company_id, period, PERIOD_TYPE, r[0], r[1], r[2], currency,
              r[3], SOURCE_KIND, rel_src, r[4], now) for r in rows],
        )

        # Journal: strömmande iterparse + batchad insert (5000 rader/batch).
        # Idempotens: rensa per (company_id, period) — INTE per source_file.
        # En SAF-T-fil är YTD; en senare månadsfil måste ersätta tidigare
        # filers överlappande månader. Källfilsnyckling missade det och
        # dubbelräknade verifikat (load_sie undviker det via period-nyckling).
        # Period-setet måste vara känt före DELETE → journalen läses i två
        # pass (1: perioder, 2: insert).
        # Cutoff: om --period är satt skippas journal-rader med
        # jp > period_override (FY-filer kan innehålla framtida tomma månader).
        journal_rows_loaded = 0
        journal_skipped = 0
        journal_periods: set[str] = set()
        if include_journal:
            # Pass 1: vilka perioder täcker filens journal?
            for j in iter_saft_journal(path, parsed["ns"]):
                jp = _journal_period(j, period)
                if period_override and jp > period_override:
                    continue
                journal_periods.add(jp)
            if journal_periods:
                placeholders = ",".join(["%s"] * len(journal_periods))
                con.execute(
                    f"""DELETE FROM fact_journal_saft
                        WHERE company_id = %s AND period IN ({placeholders})""",
                    [company_id, *sorted(journal_periods)],
                )
            # Pass 2: strömma in raderna batchvis.
            batch: list[tuple] = []
            for j in iter_saft_journal(path, parsed["ns"]):
                jp = _journal_period(j, period)
                if period_override and jp > period_override:
                    journal_skipped += 1
                    continue
                debit = j["debit"] or 0.0
                credit = j["credit"] or 0.0
                amt = debit - credit
                batch.append((
                    company_id, jp,
                    j["journal_id"], j["journal_desc"],
                    j["transaction_id"], j["transaction_date"], j["transaction_desc"],
                    j["line_no"], j["record_id"], j["account_code"],
                    debit, credit, amt, j["line_desc"],
                    currency, rel_src, now,
                ))
                if len(batch) >= JOURNAL_BATCH:
                    con.executemany(_INSERT_JOURNAL_SAFT, batch)
                    journal_rows_loaded += len(batch)
                    batch.clear()
            if batch:
                con.executemany(_INSERT_JOURNAL_SAFT, batch)
                journal_rows_loaded += len(batch)
            if journal_periods:
                db.sync_dim_period(con, sorted(journal_periods))

        con.execute(
            """INSERT INTO load_history
               (company_id, period, source_kind, source_file, rows_loaded,
                sum_amount, statement_type_present, status, message, loaded_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [company_id, period, SOURCE_KIND, rel_src,
             len(rows) + journal_rows_loaded, total, True,
             "ok",
             f"sum_bs={total_bs:.2f} sum_is={total_is:.2f} "
             f"journal_rows={journal_rows_loaded} "
             f"journal_periods={len(journal_periods)}", now],
        )
        con.execute("COMMIT")
    except Exception as e:
        con.execute("ROLLBACK")
        log("ERROR", company_id, f"DB-fel {path.name}: {e}")
        return "error"

    journal_msg = f" JOURNAL={journal_rows_loaded}({len(journal_periods)} mån)" if include_journal else ""
    cutoff_msg = (f"  CUTOFF<= {period_override}: skippade journal={journal_skipped}"
                  if include_journal and period_override and journal_skipped else "")
    log("OK", company_id, f"{path.name}  rader={len(rows)}{journal_msg} sum={total:.2f}{cutoff_msg}")
    return "ok"


_INSERT_JOURNAL_SAFT = """
INSERT INTO fact_journal_saft
(company_id, period, journal_id, journal_description,
 transaction_id, transaction_date, transaction_description,
 line_no, record_id, account_code,
 debit_amount, credit_amount, amount, line_description,
 currency, source_file, loaded_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def discover_files(source_dir: Path) -> list[Path]:
    """Hitta SAF-T XML-filer direkt i source_dir (inte i Referens/)."""
    if not source_dir.exists():
        return []
    return sorted(f for f in source_dir.iterdir()
                  if f.is_file() and f.suffix.lower() == ".xml")


def discover_files_for_period(base_path: Path, period: str,
                              countries: list[str] | None = None) -> list[Path]:
    """Hitta alla SAF-T XML-filer för perioden under extracted/{period}/{Country}/."""
    countries = countries or list(COUNTRY_DIRS)
    found: list[Path] = []
    for cc in countries:
        sub = COUNTRY_DIRS.get(cc)
        if not sub:
            continue
        found.extend(discover_files(base_path / "extracted" / period / sub))
    return found


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ladda SAF-T XML (Norge + Danmark) till Postgres (fact_balances)."
    )
    parser.add_argument("--period", default=None,
                        help="YYYYMM. Override för period (default: härleds från XML-Header)")
    parser.add_argument("--source-dir", default=None,
                        help="Mapp att läsa från (default: scanna både "
                             "extracted/{period}/Norway och .../Denmark under base_path)")
    parser.add_argument("--country", choices=sorted(NS_BY_COUNTRY), default=None,
                        help="Begränsa till ett land (NO eller DK). Default: båda.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--include-journal", default=True,
                        action=argparse.BooleanOptionalAction,
                        help="Ladda även GeneralLedgerEntries till "
                             "fact_journal_saft. Default: aktivt. "
                             "--no-include-journal stänger av (SAF-T-filer är ofta stora).")
    parser.add_argument("--override", nargs="*", type=int, default=None, metavar="ID",
                        help="Skriv över befintlig SAFT inom FY. "
                             "--override = global; --override 134 196 = bara dessa bolag.")
    args = parser.parse_args()

    period_for_log = args.period or prev_month_period()
    begin_run("load_saft.py", period_for_log)
    log("START", "load_saft.py",
        f"period={args.period or '(auto)'} country={args.country or 'NO+DK'} "
        f"dry_run={args.dry_run} journal={args.include_journal}")

    cfg = load_config()
    base_path = Path(cfg["base_path"])

    if args.source_dir:
        source_dir = Path(args.source_dir)
        log("INFO", "scan", f"Söker SAF-T i {source_dir}")
        files = discover_files(source_dir)
    else:
        countries = [args.country] if args.country else None
        country_label = args.country or "NO+DK"
        log("INFO", "scan",
            f"Söker SAF-T i extracted/{period_for_log}/{{Norway,Denmark}} "
            f"(filter: {country_label})")
        files = discover_files_for_period(base_path, period_for_log, countries)

    if not files:
        log("WARN", "scan", "Inga .xml-filer hittades")
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
                               dry_run=args.dry_run,
                               include_journal=args.include_journal,
                               override=args.override)
            counts[status] = counts.get(status, 0) + 1
    finally:
        con.close()

    log("DONE", "load_saft.py",
        f"{counts['ok']} OK  {counts['warn']} WARN  {counts['skip']} SKIP  {counts['error']} ERROR")


if __name__ == "__main__":
    main()
