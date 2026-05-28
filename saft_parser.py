"""Kanonisk SAF-T-parsning (Norge + Danmark) — delad parse-/valideringslogik.

Ren parse- och valideringslogik: ingen databas, inga loader-specifika DB-rader.
Speglar sie_parser.py. SAF-T är XML med namespace; roten bär
`urn:StandardAuditFile-Taxation-Financial:NO` (Norge) eller `:DK` (Danmark) och
ALLA element-sökningar prefixas med `{ns}`. Land och defaultvaluta härleds ur
namespace.

Tidigare låg parse_saft + iter_saft_journal direkt i load_saft.py (plus en
namespace-blind variant i process_norway.py och en första-matchnings-variant i
load_history). Den här modulen är enda källan — namespace-rigorös och med orgnr
scopat till Header/Company (aldrig AuditFileSender).

Designval (Etapp 4): de vendorade xsdata-klasserna i saft_schema_no/ används som
auktoritativ XSD-spec och som test-tids-validator (se tests/), INTE som
runtime-parser — pure-python-bindning av 30k+ journalelement mätte ~11x
långsammare än streamande iterparse och hade underminerat COPY-optimeringen.
Runtime-parsningen är därför streamande iterparse, kodad mot xsdata-specen.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

NS_BY_COUNTRY = {
    "NO": "urn:StandardAuditFile-Taxation-Financial:NO",
    "DK": "urn:StandardAuditFile-Taxation-Financial:DK",
}
NS_TO_COUNTRY = {v: k for k, v in NS_BY_COUNTRY.items()}
DEFAULT_CURRENCY = {"NO": "NOK", "DK": "DKK"}


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
                    value_date = _parse_iso_date(_t(line, "ValueDate", ns))
                    debit_elem = line.find(f"{{{ns}}}DebitAmount")
                    credit_elem = line.find(f"{{{ns}}}CreditAmount")
                    debit = _amount(_t(debit_elem, "Amount", ns)) if debit_elem is not None else 0.0
                    credit = _amount(_t(credit_elem, "Amount", ns)) if credit_elem is not None else 0.0
                    yield {
                        "journal_id": j_id, "journal_desc": j_desc,
                        "transaction_id": tx_id, "transaction_date": tx_date,
                        "transaction_desc": tx_desc,
                        "value_date": value_date,
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
    """YYYYMM för en journal-LINJE — från ValueDate (linjenivå), annars
    TransactionDate (transaktionsnivå), annars fallback.

    SAF-T periodiserar per linje via ValueDate. TransactionDate är verifikatets
    bokföringsdag och kan klumpa hela årets linjer i en månad (Tripletex bokar
    t.ex. årets avskrivningar i jan) — den får inte styra perioden."""
    d = j.get("value_date") or j["transaction_date"]
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
