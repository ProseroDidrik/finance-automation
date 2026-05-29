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

# Parsningen är konsoliderad i saft_parser.py (Etapp 4). Re-exporteras här så att
# load_history_sie_saft.py m.fl. som importerar load_saft.parse_saft /
# iter_saft_journal fortsätter fungera oförändrat.
from saft_parser import (  # noqa: F401
    DEFAULT_CURRENCY,
    NS_BY_COUNTRY,
    NS_TO_COUNTRY,
    _detect_namespace,
    _journal_period,
    derive_fy_range,
    derive_period,
    iter_saft_journal,
    normalize_orgnr,
    parse_saft,
    statement_type_from_code,
    validate_xsd,
)

SOURCE_KIND = "SAFT"
PERIOD_TYPE = "ytd"

# Underkataloger (under extracted/{period}/) som scannas för SAF-T-XML
COUNTRY_DIRS = {"NO": "Norway", "DK": "Denmark"}

# SAF-T-filer som saknar RegistrationNumber i Header: filnamnssubsträng → company_id
# (Säkerhetsnät — normalt löses Actas via orgnr-lookup på "29 14 36 25".)
FILENAME_OVERRIDES: dict[str, int] = {
    "081_Actas": 81,  # Actas DK
}


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
              override: list[int] | None = None, force: bool = False) -> str:
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

    # XSD-valideringsgrind (bypassbar med --force). Endast NO 1.30 har vendorad
    # XSD; NO 1.20 + DK saknar XSD och 'skipped' (best-effort, ingen WARN-spam).
    # 'invalid' blockerar utan --force. orgnr/period är strukturella krav ovan
    # och INTE bypassbara här.
    xsd_status, xsd_errors = validate_xsd(path)
    if xsd_status == "invalid":
        gate_msg = (f"{path.name}: XSD-validering ({len(xsd_errors)} fel, "
                    f"ex: {xsd_errors[0][:120]})")
        if force:
            log("WARN", company_id, f"{gate_msg}  [--force: laddar ändå]")
        else:
            log("ERROR", company_id,
                f"{gate_msg}  (kör med --force för att ladda ändå)")
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
                """DELETE FROM fact_saft_analysis
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

        # Dimensioner: upserta axel- + medlemsnamn ur MasterFiles/AnalysisTypeTable.
        # Best-effort (ON CONFLICT) — namnen kan saknas (NO 1.20/DK 1.0 → tom lista).
        type_rows, member_rows = dim_analysis_rows(
            parsed.get("analysis_types", []), company_id, now)
        if type_rows:
            con.executemany(
                """INSERT INTO dim_analysis_type
                   (company_id, source_format, analysis_type, description, loaded_at)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (company_id, source_format, analysis_type)
                   DO UPDATE SET description = EXCLUDED.description,
                                 loaded_at = EXCLUDED.loaded_at""",
                type_rows)
        if member_rows:
            con.executemany(
                """INSERT INTO dim_analysis_member
                   (company_id, source_format, analysis_type, analysis_id,
                    description, loaded_at)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (company_id, source_format, analysis_type, analysis_id)
                   DO UPDATE SET description = EXCLUDED.description,
                                 loaded_at = EXCLUDED.loaded_at""",
                member_rows)

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
        journal_vdate_fallback = 0
        analysis_rows_loaded = 0
        journal_periods: set[str] = set()
        if include_journal:
            # Pass 1: vilka perioder täcker filens journal? (ValueDate-härlett)
            for j in iter_saft_journal(path, parsed["ns"]):
                jp = _journal_period(j, period)
                if period_override and jp > period_override:
                    continue
                journal_periods.add(jp)
            if journal_periods:
                placeholders = ",".join(["%s"] * len(journal_periods))
                # Journal OCH analys nyckas på SAMMA ValueDate-härledda period-set
                # → idempotens-paritet (annars ackumulerar --override dubbletter).
                con.execute(
                    f"""DELETE FROM fact_journal_saft
                        WHERE company_id = %s AND period IN ({placeholders})""",
                    [company_id, *sorted(journal_periods)],
                )
                con.execute(
                    f"""DELETE FROM fact_saft_analysis
                        WHERE company_id = %s AND period IN ({placeholders})""",
                    [company_id, *sorted(journal_periods)],
                )
            # Pass 2: journal via COPY; analys buffras och COPY:as separat efteråt
            # (psycopg tillåter en COPY i taget per anslutning). line_rows ger
            # journal- och analystupler SAMMA jp → analysen periodiseras aldrig
            # annorlunda än journalen (b711832-skydd).
            analysis_buf: list[tuple] = []
            cur = con.cursor()
            try:
                with cur.copy(_COPY_JOURNAL_SAFT) as cp:
                    for j in iter_saft_journal(path, parsed["ns"]):
                        if j.get("value_date") is None:
                            journal_vdate_fallback += 1
                        jt, ats, jp, skipped = line_rows(
                            j, company_id, currency, rel_src, now, period,
                            period_cutoff=period_override)
                        if skipped:
                            journal_skipped += 1
                            continue
                        cp.write_row(jt)
                        analysis_buf.extend(ats)
                        journal_rows_loaded += 1
            finally:
                cur.close()
            if analysis_buf:
                cur2 = con.cursor()
                try:
                    with cur2.copy(_COPY_ANALYSIS_SAFT) as cp:
                        for row in analysis_buf:
                            cp.write_row(row)
                            analysis_rows_loaded += 1
                finally:
                    cur2.close()
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
             f"journal_periods={len(journal_periods)} "
             f"analysis_rows={analysis_rows_loaded}", now],
        )
        con.execute("COMMIT")
    except Exception as e:
        con.execute("ROLLBACK")
        log("ERROR", company_id, f"DB-fel {path.name}: {e}")
        return "error"

    journal_msg = (f" JOURNAL={journal_rows_loaded}({len(journal_periods)} mån)"
                   f" ANALYS={analysis_rows_loaded}" if include_journal else "")
    cutoff_msg = (f"  CUTOFF<= {period_override}: skippade journal={journal_skipped}"
                  if include_journal and period_override and journal_skipped else "")
    if include_journal and journal_vdate_fallback:
        log("WARN", company_id,
            f"{path.name}  {journal_vdate_fallback} journal-linjer saknar ValueDate "
            f"— periodiserade på TransactionDate")
    log("OK", company_id, f"{path.name}  rader={len(rows)}{journal_msg} sum={total:.2f}{cutoff_msg}")
    return "ok"


_COPY_JOURNAL_SAFT = """
COPY fact_journal_saft
(company_id, period, journal_id, journal_description,
 transaction_id, transaction_date, transaction_description,
 line_no, record_id, account_code,
 debit_amount, credit_amount, amount, line_description,
 currency, source_file, loaded_at)
FROM STDIN
"""

_COPY_ANALYSIS_SAFT = """
COPY fact_saft_analysis
(company_id, period, transaction_id, line_no, record_id, account_code,
 analysis_type, analysis_id, amount, currency, source_file, loaded_at)
FROM STDIN
"""


def dim_analysis_rows(analysis_types, company_id, now, source_format="SAFT"):
    """(analysis_type, type_desc, analysis_id, id_desc)-lista (från parse_saft) →
    (type_rows, member_rows), deduplicerade. Ingen DB.

    type_rows:   (company_id, source_format, analysis_type, description, loaded_at)
    member_rows: (company_id, source_format, analysis_type, analysis_id, description, loaded_at)
    """
    types: dict = {}
    members: dict = {}
    for atype, tdesc, aid, idesc in analysis_types:
        if atype is None:
            continue
        types[atype] = (company_id, source_format, atype, tdesc, now)
        if aid is not None:
            members[(atype, aid)] = (company_id, source_format, atype, aid, idesc, now)
    return list(types.values()), list(members.values())


def line_rows(line, company_id, currency, rel_src, now, fallback_period,
              period_cutoff=None):
    """Bygg (journal_tuple, analysis_tuples, jp, skipped) för EN journal-linje.

    jp härleds EN gång via _journal_period (ValueDate per linje → TransactionDate-
    fallback). BÅDE journaltupeln och alla analystupler får samma jp → analysen
    kan inte periodiseras annorlunda än journalen (skydd mot b711832-regression).
    period_cutoff: om satt och jp > cutoff → skipped=True (journal + analys droppas).
    """
    jp = _journal_period(line, fallback_period)
    if period_cutoff is not None and jp > period_cutoff:
        return None, [], jp, True
    debit = line["debit"] or 0.0
    credit = line["credit"] or 0.0
    amount = debit - credit
    journal_tuple = (
        company_id, jp,
        line["journal_id"], line["journal_desc"],
        line["transaction_id"], line["transaction_date"], line["transaction_desc"],
        line["line_no"], line["record_id"], line["account_code"],
        debit, credit, amount, line["line_desc"],
        currency, rel_src, now,
    )
    analysis_tuples = [
        (company_id, jp, line["transaction_id"], line["line_no"], line["record_id"],
         line["account_code"], atype, aid, amount, currency, rel_src, now)
        for (atype, aid) in line.get("analysis", [])
    ]
    return journal_tuple, analysis_tuples, jp, False


def group_analysis_by_period(lines, company_id, currency, rel_src, now,
                             fallback_period, period_cutoff=None):
    """Gruppera analystupler per period ur journal-linjer (ingen DB).

    Återanvänder line_rows → varje analysrad ärver linjens ValueDate-period (jp).
    Returnerar dict[period -> list[analysis_tuple]]. Linjer med jp > period_cutoff
    skippas (samma cutoff som load_file). Kärnan i historik-backfillen, testbar
    utan databas."""
    by_period: dict[str, list[tuple]] = {}
    for line in lines:
        _jt, ats, jp, skipped = line_rows(
            line, company_id, currency, rel_src, now, fallback_period,
            period_cutoff=period_cutoff)
        if skipped or not ats:
            continue
        by_period.setdefault(jp, []).extend(ats)
    return by_period


def backfill_file_analysis(con, path, base_path, period_override, orgnr_lookup,
                           *, dry_run=False):
    """Backfilla BARA fact_saft_analysis för en (historisk) SAF-T-fil.

    Rör ALDRIG fact_journal_saft/fact_balances — journalen är redan laddad och
    korrekt periodiserad. Commit per period (B1ms-säkert, idempotent,
    återstartbar). period/cutoff härleds som i load_file → analysperioder == den
    befintliga journalens perioder.
    """
    try:
        parsed = parse_saft(path)
    except Exception as e:
        log("ERROR", path.name, f"Läsfel: {e}")
        return "error"

    country = parsed.get("country")
    if country not in NS_BY_COUNTRY:
        log("ERROR", path.name, f"Okänd SAF-T-namespace ({parsed.get('ns')!r})")
        return "error"

    # Företag (samma logik som load_file)
    orgnr_raw = parsed.get("orgnr")
    company_id = None
    if not orgnr_raw:
        for substr, cid in FILENAME_OVERRIDES.items():
            if substr in path.name:
                company_id = cid
                break
        if company_id is None:
            log("ERROR", path.name, "Saknar Header/Company/RegistrationNumber")
            return "error"
    else:
        hit = orgnr_lookup.get(normalize_orgnr(orgnr_raw))
        if not hit:
            log("ERROR", path.name, f"OrgNr {orgnr_raw} saknas i dim_company")
            return "error"
        company_id, _name = hit

    period = derive_period(parsed, period_override)
    if not period:
        log("ERROR", company_id, f"Kunde inte härleda period från {path.name}")
        return "error"
    currency = parsed.get("currency") or DEFAULT_CURRENCY[country]
    rel_src = db.relpath_from_base(path, base_path)
    now = datetime.now()

    # En journal-iter → analys grupperad per period (ValueDate-bunden).
    by_period = group_analysis_by_period(
        iter_saft_journal(path, parsed["ns"]),
        company_id, currency, rel_src, now,
        fallback_period=period, period_cutoff=period_override)
    type_rows, member_rows = dim_analysis_rows(
        parsed.get("analysis_types", []), company_id, now)
    total = sum(len(v) for v in by_period.values())

    if dry_run:
        log("OK", company_id,
            f"[DRY] {path.name}  analys={total} i {len(by_period)} perioder "
            f"dim_type={len(type_rows)} dim_member={len(member_rows)} (journal orörd)")
        return "ok"

    # Dim-upsert (egen liten transaktion). SQL dupliceras medvetet från load_file
    # för att hålla load_file orört (noll regressionsrisk på månadsladdaren).
    con.execute("BEGIN")
    try:
        if type_rows:
            con.executemany(
                """INSERT INTO dim_analysis_type
                   (company_id, source_format, analysis_type, description, loaded_at)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (company_id, source_format, analysis_type)
                   DO UPDATE SET description = EXCLUDED.description,
                                 loaded_at = EXCLUDED.loaded_at""",
                type_rows)
        if member_rows:
            con.executemany(
                """INSERT INTO dim_analysis_member
                   (company_id, source_format, analysis_type, analysis_id,
                    description, loaded_at)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (company_id, source_format, analysis_type, analysis_id)
                   DO UPDATE SET description = EXCLUDED.description,
                                 loaded_at = EXCLUDED.loaded_at""",
                member_rows)
        con.execute("COMMIT")
    except Exception as e:
        con.execute("ROLLBACK")
        log("ERROR", company_id, f"dim-upsert-fel {path.name}: {e}")
        return "error"

    # Analys: en transaktion PER period (B1ms-säkert + idempotent + återstartbar).
    loaded = 0
    for p in sorted(by_period):
        rows = by_period[p]
        con.execute("BEGIN")
        try:
            con.execute(
                "DELETE FROM fact_saft_analysis WHERE company_id = %s AND period = %s",
                [company_id, p])
            cur = con.cursor()
            try:
                with cur.copy(_COPY_ANALYSIS_SAFT) as cp:
                    for row in rows:
                        cp.write_row(row)
            finally:
                cur.close()
            db.sync_dim_period(con, [p])
            con.execute("COMMIT")
            loaded += len(rows)
        except Exception as e:
            con.execute("ROLLBACK")
            log("ERROR", company_id, f"analys-fel {path.name} period {p}: {e}")
            return "error"

    log("OK", company_id,
        f"{path.name}  analys={loaded} i {len(by_period)} perioder (journal orörd)")
    return "ok"


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
    parser.add_argument("--force", action="store_true",
                        help="Ladda även om XSD-valideringsgrinden fallerar "
                             "(degraderar 'invalid' till WARN). Gäller NO 1.30.")
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
        # init_schema kräver DDL → körs egentligen av `py db.py` med admin-rollen
        # innan ETL-laddningar. Vi behåller anropet defensivt för lokal dev där
        # samma user gör allt, men under T2 (separata ETL/admin-roller) failar
        # det med InsufficientPrivilege — det är inte ett verkligt fel, det
        # betyder bara att schema redan är initierat av admin.
        try:
            db.init_schema(con)
        except Exception as e:
            if "InsufficientPrivilege" in type(e).__name__ \
                    or "permission denied" in str(e).lower():
                log("INFO", "schema",
                    "Hoppar over init_schema (ETL-rollen utan DDL — "
                    "antar att schema redan finns)")
                con.raw.rollback()  # rensa failed transaction
            else:
                raise
        orgnr_lookup = build_orgnr_lookup(con)
        if not orgnr_lookup:
            log("ERROR", "scan", "Inga bolag med orgnr i dim_company — kör 'py db.py' först")
            return
        counts = {"ok": 0, "warn": 0, "skip": 0, "error": 0}
        for f in files:
            status = load_file(con, f, base_path, args.period, orgnr_lookup,
                               dry_run=args.dry_run,
                               include_journal=args.include_journal,
                               override=args.override, force=args.force)
            counts[status] = counts.get(status, 0) + 1
    finally:
        con.close()

    log("DONE", "load_saft.py",
        f"{counts['ok']} OK  {counts['warn']} WARN  {counts['skip']} SKIP  {counts['error']} ERROR")


if __name__ == "__main__":
    main()
