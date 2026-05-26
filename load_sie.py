"""Ladda SIE-filer (Sverige) till fact_balances i Postgres.

#UB/#RES är YTD-baserade; #PSALDO är månadsrörelse. För varje fil:
  #UB 0 <konto> <belopp>            → BS-konto, utgående balans (YTD)
  #RES 0 <konto> <belopp>           → IS-konto, ackumulerat resultat (YTD)
  #PSALDO 0 <YYYYMM> <konto> {} <belopp>
                                    → MÅNADSRÖRELSE för kontot den månaden
                                      (INTE YTD — summan över årets perioder
                                      = #RES 0). Endast {}-totalen laddas;
                                      dimensionssplit-rader hoppas över.
  #KONTO <konto> "<namn>"           → kontoplan
  #ORGNR / #FNAMN / #RAR / #GEN     → metadata

Period-härledning (#PSALDO är det enda fält i SIE som faktiskt anger
"data fram till och med"; #GEN är exportdatum, #RAR är räkenskapsårets slut):
  - Om filen har #PSALDO används max(#PSALDO) som period för UB/RES —
    det är filens faktiska data-through.
  - --period fungerar som lägstagräns: ERROR om max(#PSALDO) < --period
    (filen saknar data för begärd period). En senare PSALDO är OK; PSALDO-
    lanen fyller ändå begärd period från radens egen YYYYMM.
  - Saknar filen #PSALDO krävs --period explicit.

Två separata source_kind-laner skrivs till fact_balances:
  SOURCE_KIND='SIE'         → UB/RES, period_type='ytd', period=filens
  SOURCE_KIND='SIE_PSALDO'  → PSALDO, period_type='monthly', period=radens YYYYMM

Idempotens: senaste laddningen vinner per (company_id, period, source_kind).
"""
from __future__ import annotations

import argparse
import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

import db
from shared import begin_run, is_override_for, load_config, log, prev_month_period

SOURCE_KIND = "SIE"
SOURCE_KIND_PSALDO = "SIE_PSALDO"
SOURCE_KIND_SIE_VER = "SIE_VER"
PERIOD_TYPE = "ytd"
# SIE_PSALDO är månadsrörelse, INTE YTD — egen period_type så report_pnl.sql
# routar den genom monthly-grenen (3b) i stället för YTD-subtraktion (3a).
PERIOD_TYPE_PSALDO = "monthly"
ENCODINGS = ("utf-8-sig", "cp437", "latin-1")

# #ORGNR: orgnr är antingen en citerad sträng (norska Global-exporter skriver
# "NO 971199954 MVA" / "989 285 246 MVA" — prefix/suffix och mellanslag) eller
# ett ociterat token (svensk standard, t.ex. 556071-2340). Grupp 1 = citerat
# innehåll, grupp 2 = ociterat token; normalize_orgnr strippar allt utom siffror.
RE_ORGNR  = re.compile(r'^#ORGNR\s+(?:"([^"\r\n]*)"|(\S+))', re.IGNORECASE)
RE_FNAMN  = re.compile(r'^#FNAMN\s+"([^"]*)"', re.IGNORECASE)
RE_PROGRAM = re.compile(r'^#PROGRAM\s+"([^"]*)"', re.IGNORECASE)
RE_KONTO  = re.compile(r'^#KONTO\s+(\S+)\s+"([^"]*)"', re.IGNORECASE)
RE_UB     = re.compile(r"^#UB\s+0\s+(\S+)\s+(-?\d+(?:[.,]\d+)?)", re.IGNORECASE)
RE_RES    = re.compile(r"^#RES\s+0\s+(\S+)\s+(-?\d+(?:[.,]\d+)?)", re.IGNORECASE)
# Dynamics NAV exporterar #RES 0 som "ackumulerat över alla år" istället för
# YTD innevarande RAR. Vi läser #RES -1 (föregående RAR) för att kunna
# korrigera detta nedan, men ENDAST när #PROGRAM matchar NAV.
RE_RES_PRIOR = re.compile(r"^#RES\s+-1\s+(\S+)\s+(-?\d+(?:[.,]\d+)?)", re.IGNORECASE)
# Endast {}-totalen laddas — INTE dimensionssplit-rader ({1 "200"} etc).
# Tidigare \{[^}]*\} matchade båda → SIE_PSALDO dubbel-/trippelräknades för
# bolag som dim-taggar #PSALDO (23, 75, 186). Dim-splittar summerar till
# {}-totalen, så bara totalen ska laddas.
RE_PSALDO = re.compile(
    r"^#PSALDO\s+0\s+(\d{6})\s+(\S+)\s+\{\s*\}\s+(-?\d+(?:[.,]\d+)?)",
    re.IGNORECASE,
)
# Diagnostik-variant: matchar #PSALDO med VALFRITT objektlist-innehåll och
# fångar brace-innehållet (grupp 3) så psaldo_dim_coverage kan skilja
# {}-totaler från dimensionssplittar. Används inte av laddningen.
RE_PSALDO_ANY = re.compile(
    r"^#PSALDO\s+0\s+(\d{6})\s+(\S+)\s+\{([^}]*)\}\s+(-?\d+(?:[.,]\d+)?)",
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
    r'(?:\s+(\d{8}))?'                                    # transdat (ociterat YYYYMMDD)
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
        "orgnr": None, "fnamn": None, "program": None, "konto": {},
        "ub": [], "res": [], "res_prior": [], "psaldo": [],
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
            out["orgnr"] = m.group(1) if m.group(1) is not None else m.group(2)
        elif m := RE_FNAMN.match(line):
            out["fnamn"] = m.group(1)
        elif m := RE_PROGRAM.match(line):
            out["program"] = m.group(1)
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
        elif m := RE_RES_PRIOR.match(line):
            try:
                out["res_prior"].append((m.group(1), float(m.group(2).replace(",", "."))))
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
                             rel_src: str, now: datetime,
                             period_cutoff: str | None = None
                             ) -> tuple[list[tuple], set[str], int]:
    """Plana ut vouchers → rader för fact_journal_sie.

    period_cutoff: om satt, skippa vouchers vars period (YYYYMM) > cutoff.
    Returnerar (rows, periods, skipped_periods_count).
    """
    konto = parsed["konto"]
    rows: list[tuple] = []
    periods: set[str] = set()
    skipped = 0
    for v in parsed["vouchers"]:
        d = v["date"]  # 'YYYYMMDD'
        period = d[:6]
        if period_cutoff and period > period_cutoff:
            skipped += 1
            continue
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
    return rows, periods, skipped


def fy_periods(fy_start: str, period: str) -> list[str]:
    """Lista kalendermånader 'YYYYMM' från fy_start t.o.m. period (inklusive).

    Antar kalenderårs-progression. Anroparen ska redan ha avvisat brutet
    räkenskapsår (fy_start som inte slutar på '01').
    """
    assert fy_start <= period, f"fy_periods: fy_start {fy_start!r} > period {period!r}"
    out: list[str] = []
    y, m = int(fy_start[:4]), int(fy_start[4:6])
    while True:
        p = f"{y:04d}{m:02d}"
        out.append(p)
        if p >= period:
            break
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def cumulate_ytd(monthly_rows: Iterable[tuple[str, str, float]],
                 periods: list[str]) -> list[tuple[str, str, float]]:
    """Kumulera månadsrörelse → YTD-saldo per konto.

    monthly_rows: iterable av (account_code, period, amount) — månadsrörelse.
    periods:      ordnad lista FY-perioder 'YYYYMM' (från fy_periods()).

    Returnerar list[(account_code, period, ytd_amount)]. Varje konto får en rad
    för varje period FRÅN sin första aktivitetsmånad och framåt (carry-forward),
    så att report_pnl.sql:s YTD-diff fungerar även för en månad utan rörelse.
    Tecknet bevaras (SIE-konvention — samma som fact_journal_sie).
    Rader med samma (account_code, period) summeras — funktionen är robust
    oavsett om indata redan är aggregerad eller ej.
    """
    by_acct: dict[str, dict[str, float]] = {}
    for account_code, p, amount in monthly_rows:
        acct = by_acct.setdefault(account_code, {})
        acct[p] = acct.get(p, 0.0) + amount

    period_index = {p: i for i, p in enumerate(periods)}
    out: list[tuple[str, str, float]] = []
    for account_code, mvm in by_acct.items():
        active = [period_index[p] for p in mvm if p in period_index]
        if not active:
            continue
        running = 0.0
        for i in range(min(active), len(periods)):
            running += mvm.get(periods[i], 0.0)
            out.append((account_code, periods[i], running))
    return out


# Kontoklass 3–8 = resultaträkning (IS). 1–2 = balansräkning (BS) och kan inte
# YTD-kumuleras utan korrekt ingående balans — skippas i SIE_VER.
IS_ACCOUNT_CLASSES = ("3", "4", "5", "6", "7", "8")

# Länder vars SIE-bolag får SIE_VER-syntes. Sverige + CA (bolag med svenskt
# orgnr men annan koncernklassning, t.ex. 49, 162) — speglar SIE_VER-posterna
# i IMP_KINDS_BY_COUNTRY (db.py). Norska SIE-bolag och CENTR exkluderas:
# syntesen antar svensk kontoplan och kalenderår.
SIE_VER_COUNTRIES = ("Sweden", "CA")

# Hybrid-fallback-tröskel: när |journal-cum jan..period − #RES YTD| är större än
# detta, betraktas journalen som icke-täckande och vi använder #RES istället.
# Bakgrund: 4–5 SE-bolag (14/18/41/152) levererar SIE där löner och andra
# personalkostnader rapporteras som #RES men *aldrig som #VER/#TRANS*
# (lönesystemet exporterar inte verifikatrader). Empirisk validering 2026-05-25:
# 32/32 stickprov där SIE och SIE_VER skiljer sig >10 % — Mercur följer alltid
# #RES. Tröskeln 5 % relativt + 100 absolut fångar konton där lönerna saknas
# helt utan att false-positive:a på små periodiseringsdiffar.
SIE_VER_FALLBACK_REL = 0.05
SIE_VER_FALLBACK_ABS = 100.0


def synthesize_sie_ver(con, company_id: int, fy_start: str, fy_end: str,
                       period: str, rel_src: str, now: datetime,
                       parsed_res: list[tuple[str, float]] | None = None,
                       parsed_konto: dict[str, str] | None = None,
                       ) -> tuple[int, int]:
    """Syntetisera SIE_VER-rader (YTD-saldon) från fact_journal_sie + #RES-fallback.

    Anropas inom den öppna transaktionen i load_file, EFTER att journalraderna
    skrivits — läser därför både den aktuella filens verifikat och tidigare
    laddade månader. Aggregerar verifikat per (konto, period), kumulerar till
    YTD och skriver source_kind='SIE_VER'. Bara IS-konton (kontoklass 3–8).

    **Hybrid-fallback per konto** (när parsed_res anges): för IS-konton i #RES
    där journal-cum jan..period avviker mer än SIE_VER_FALLBACK_REL × |#RES|
    (med abs golv SIE_VER_FALLBACK_ABS) anses journalen icke-täckande. Då
    skrivs en jämnt fördelad YTD-serie över FY-perioderna fram till `period`:

        YTD[i] = #RES × (i+1) / len(periods)

    Jämnfördelningen är **en fabrikation** — vi vet inte den faktiska månads-
    fördelningen för konton som saknar verifikat. För compare-script och YTD-
    rapporter blir summa jan..period rätt. Månadsrapporter kommer visa
    löner utsmetade jämnt över hela FY:t istället för verklig payroll-rytm.

    DELETE täcker hela FY:t (fy_start..fy_end) → idempotent och rensar även
    ev. stale senare-månadsrader. INSERT skrivs bara för fy_start..period.

    Returnerar (n_rows_skrivna, n_fallback_konton). n_fallback_konton = antal
    IS-konton där #RES-fallback aktiverades.
    """
    periods = fy_periods(fy_start, period)

    # Ett pass över fact_journal_sie: månadsrörelse + kontonamn per IS-konto.
    rows = con.execute(
        """SELECT account_code, period,
                  SUM(amount)       AS amount,
                  MAX(account_name) AS account_name
           FROM fact_journal_sie
           WHERE company_id = %s
             AND period BETWEEN %s AND %s
             AND LEFT(account_code, 1) = ANY(%s)
           GROUP BY account_code, period""",
        [company_id, fy_start, period, list(IS_ACCOUNT_CLASSES)],
    ).fetchall()

    journal = [(code, p, amount) for code, p, amount, _name in rows]
    names: dict[str, str | None] = {}
    for code, _p, _amount, account_name in rows:
        if account_name is not None or code not in names:
            names[code] = account_name

    # --- Hybrid-fallback: identifiera konton där #RES tas över för journal ---
    fallback_konton: set[str] = set()
    fallback_rows: list[tuple[str, str, float]] = []
    if parsed_res:
        # Aggregera #RES till {code: ytd_value} och filtrera IS-konton.
        res_by_code: dict[str, float] = {}
        for code, amt in parsed_res:
            if code and code[:1] in IS_ACCOUNT_CLASSES:
                res_by_code[code] = res_by_code.get(code, 0.0) + amt
        # Journal-cum jan..period per konto.
        journal_cum: dict[str, float] = {}
        for code, _p, amount in journal:
            journal_cum[code] = journal_cum.get(code, 0.0) + amount
        n_periods = len(periods)
        for code, res_ytd in res_by_code.items():
            jc = journal_cum.get(code, 0.0)
            if abs(res_ytd) < SIE_VER_FALLBACK_ABS:
                # #RES är nära noll — låt journal styra (eller skip om även 0).
                continue
            tol = max(SIE_VER_FALLBACK_ABS,
                      SIE_VER_FALLBACK_REL * abs(res_ytd))
            if abs(jc - res_ytd) <= tol:
                continue  # journal matchar #RES tillräckligt — använd journal.
            fallback_konton.add(code)
            # Jämn YTD-fördelning över FY-perioderna fram till `period`.
            for i, p in enumerate(periods):
                fallback_rows.append(
                    (code, p, res_ytd * (i + 1) / n_periods),
                )
            if code not in names and parsed_konto:
                names[code] = parsed_konto.get(code)

    # Idempotens: rensa hela FY:t innan INSERT.
    con.execute(
        """DELETE FROM fact_balances
           WHERE company_id = %s AND source_kind = %s
             AND period BETWEEN %s AND %s""",
        [company_id, SOURCE_KIND_SIE_VER, fy_start, fy_end],
    )

    # Kumulera journal — exkludera fallback-konton (de får #RES-fördelning).
    journal_for_ytd = [(code, p, amt) for code, p, amt in journal
                       if code not in fallback_konton]
    ytd = cumulate_ytd(journal_for_ytd, periods)
    ytd.extend(fallback_rows)
    if not ytd:
        return 0, 0

    idx_per_period: dict[str, int] = {}
    insert_rows: list[tuple] = []
    for account_code, p, amount in ytd:
        idx_per_period[p] = idx_per_period.get(p, 0) + 1
        insert_rows.append((
            company_id, p, PERIOD_TYPE, account_code, names.get(account_code),
            amount, "SEK", "IS", SOURCE_KIND_SIE_VER, rel_src,
            idx_per_period[p], now,
        ))
    con.executemany(
        """INSERT INTO fact_balances
           (company_id, period, period_type, account_code, account_name,
            amount, currency, statement_type, source_kind, source_file,
            row_index, loaded_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        insert_rows,
    )
    return len(insert_rows), len(fallback_konton)


def derive_fy_range(parsed: dict, period: str) -> tuple[str, str]:
    """Räkenskapsårets (start_period, end_period) som 'YYYYMM'.

    Härleds primärt från #RAR 0 (start- och slutdatum YYYYMMDD). Det är det enda
    rätta sättet för bolag med brutet räkenskapsår. Fallback: kalenderår från
    period:t självt (för filer som saknar #RAR).
    """
    rar_start = parsed.get("rar_start")
    rar_end = parsed.get("rar_end")
    if rar_start and rar_end and len(rar_start) == 8 and len(rar_end) == 8:
        return rar_start[:6], rar_end[:6]
    year = period[:4]
    return f"{year}01", f"{year}12"


def derive_period(parsed: dict) -> str | None:
    """Endast #PSALDO är ett tillförlitligt 'data-through'-signal i SIE.

    #GEN är exportdatum (kan vara senare än datat) och #RAR är FY-slut
    (alltid YYYY1231 för månadsfiler). Båda är därför värdelösa för att
    avgöra vilken period datat representerar.
    """
    if parsed["psaldo"]:
        return max(p for p, _, _ in parsed["psaldo"])
    return None


def build_orgnr_lookup(con: db.Conn) -> dict[str, tuple[int, str, str]]:
    """orgnr_normalized → (company_id, name, country) för alla bolag med orgnr.

    SIE är ett svenskt format så valutan är alltid SEK; vi tar ingen valuta
    från dim_company här (vissa CENTR/CA-bolag har svenskt orgnr men annan
    klassad valuta). country behövs för att gata SIE_VER-syntesen till
    SIE_VER_COUNTRIES (Sverige + CA).
    """
    lookup: dict[str, tuple[int, str, str]] = {}
    for row in con.execute(
        "SELECT company_id, name, country, orgnr FROM dim_company "
        "WHERE orgnr IS NOT NULL AND orgnr <> ''"
    ).fetchall():
        cid, name, country, orgnr = row
        key = normalize_orgnr(orgnr)
        if key:
            lookup[key] = (cid, name, country)
    return lookup


RE_PATH_PERIOD = re.compile(r"(?:^|[\\/])extracted[\\/](\d{6})[\\/]", re.IGNORECASE)


def psaldo_fact_rows(psaldo_rows: list[tuple], company_id: int, currency: str,
                     rel_src: str, now: datetime) -> list[tuple]:
    """Bygg fact_balances-insertrader för SIE_PSALDO-lanen.

    psaldo_rows: tuples (period, code, name, amount, statement_type, row_index).

    #PSALDO är månadsrörelse (inte YTD) → period_type='monthly'. report_pnl.sql
    routar 'monthly'-rader genom summerings-grenen (3b); 'ytd' skulle ge
    YTD-subtraktion på en redan-månadssiffra (felaktig P&L för ~17 SE-bolag).
    Se memory reference-sie-psaldo.
    """
    return [
        (company_id, r[0], PERIOD_TYPE_PSALDO, r[1], r[2], r[3], currency,
         r[4], SOURCE_KIND_PSALDO, rel_src, r[5], now)
        for r in psaldo_rows
    ]


def check_psaldo_vs_res(parsed: dict, tol: float = 1.0) -> list[tuple]:
    """Intern konsistenskontroll: summa(#PSALDO) per konto = #RES 0.

    #PSALDO är månadsrörelse; #RES 0 är YTD-resultat. För ett resultatkonto
    ska summan av alla #PSALDO-perioder vara lika med #RES 0-värdet (samma
    SIE-teckenkonvention). En avvikelse > tol indikerar en parse-bugg eller
    en icke-standard-export (t.ex. Dynamics NAV #RES 0 = ackumulerat över
    alla år — fångas korrekt här).

    Returnerar list[(account_code, sum_psaldo, res_value, diff)] för konton
    vars |diff| > tol; tom lista = filen är internt konsistent. Detta är den
    facit-fria avstämningen — SIE-filen är sin egen facit.

    OBS: meningsfull bara när filens #PSALDO spänner från räkenskapsårets
    start. Konton som saknas i endera #PSALDO eller #RES hoppas över.
    """
    psaldo_sum: dict[str, float] = {}
    for _period, code, amt in parsed.get("psaldo", []):
        psaldo_sum[code] = psaldo_sum.get(code, 0.0) + amt
    res_by_code: dict[str, float] = {}
    for code, amt in parsed.get("res", []):
        res_by_code[code] = res_by_code.get(code, 0.0) + amt

    out: list[tuple] = []
    for code in sorted(psaldo_sum.keys() & res_by_code.keys()):
        ps, rs = psaldo_sum[code], res_by_code[code]
        diff = ps - rs
        if abs(diff) > tol:
            out.append((code, ps, rs, diff))
    return out


def check_voucher_balance(parsed: dict, tol: float = 0.005) -> list[tuple]:
    """Intern kontroll: varje verifikat ska balansera (debet = kredit).

    Summan av #TRANS-belopp i ett #VER ska vara 0. Returnerar
    list[(series, voucher_number, imbalance)] för verifikat vars |summa| > tol;
    tom lista = alla verifikat balanserar. Kräver parse_sie(..., with_journal=True).
    """
    out: list[tuple] = []
    for v in parsed.get("vouchers", []):
        imbalance = sum(t["amount"] for t in v["transes"])
        if abs(imbalance) > tol:
            out.append((v["series"], v["number"], imbalance))
    return out


def psaldo_dim_coverage(text: str) -> dict:
    """Spot-check-diagnostik för Bug 2-fixen: hittar konton som {}-only-regexen
    (RE_PSALDO) skulle tappa — dvs konton som har #PSALDO-rader men ingen
    {}-totalrad.

    RE_PSALDO laddar bara #PSALDO-rader med tom objektlista ({}). Det antar att
    varje konto med #PSALDO även har en {}-totalrad. Funktionen verifierar
    antagandet mot en faktisk fil.

    Ingen kontroll av {}-total mot summan av dim-rader görs: i SIE kan en
    transaktion bära flera dimensionsTYPER (t.ex. kostnadsställe + projekt) och
    varje typ återger HELA beloppet → Σ(dim-rader) = (antal dim-typer) × {}.
    Dim-summan har därför ingen fast relation till {}-totalen; {} ÄR totalen.

    Returnerar dict:
      total_row_count      — antal {}-totalrader (vad laddningen tar in)
      all_psaldo_accounts  — antal distinkta konton i någon #PSALDO-rad
      lost_accounts        — sorterad lista konton UTAN {}-total → skulle tappas
    """
    accounts_all: set[str] = set()
    accounts_total: set[str] = set()
    total_row_count = 0

    for raw in text.splitlines():
        m = RE_PSALDO_ANY.match(raw.lstrip())
        if not m:
            continue
        _period, account, braces, _amount = m.groups()
        accounts_all.add(account)
        if braces.strip() == "":
            total_row_count += 1
            accounts_total.add(account)

    return {
        "total_row_count": total_row_count,
        "all_psaldo_accounts": len(accounts_all),
        "lost_accounts": sorted(accounts_all - accounts_total),
    }


def load_file(con, path: Path, base_path: Path, period_override: str | None,
              orgnr_lookup: dict, *, dry_run: bool, include_journal: bool = False,
              override: list[int] | None = None) -> str:
    """Load one SIE file. Returns ok|warn|skip|error."""
    # Path-period sanity-check: om filen ligger under extracted/YYYYMM/ och
    # --period är annat värde, vägra ladda. Skyddar mot att tagga gammal
    # mars-fil som april via felaktig --source-dir (verifierat 2026-05-19
    # på bolag 105 Creab — stale 202604-rader kom från extracted/202603/).
    if period_override:
        m = RE_PATH_PERIOD.search(str(path))
        if m and m.group(1) != period_override:
            log("ERROR", path.name,
                f"Path-period mismatch: filen ligger i extracted/{m.group(1)}/ "
                f"men --period={period_override}. Flytta filen eller justera --period.")
            return "error"

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
    company_id, _name, country = hit
    currency = "SEK"

    period_derived = derive_period(parsed)  # max #PSALDO eller None
    if period_derived:
        if period_override and period_derived < period_override:
            log("ERROR", company_id,
                f"Period-mismatch i {path.name}: --period={period_override} "
                f"men filens data-through (#PSALDO max) är {period_derived}. "
                "Filen saknar data för begärd period.")
            return "error"
        # Om --period är explicit angiven och filen sträcker sig längre fram
        # (t.ex. PSALDO för hela FY men användaren vill bara ha t.o.m. mars),
        # klipper vi UB/RES/PSALDO/journal till --period nedan. UB/RES får
        # period = period_override (filen är YTD t.o.m. den månaden för de
        # konton som har transaktioner; för 'tomma' framtida månader är UB/RES
        # samma värde, så ingen informationsförlust).
        period = period_override or period_derived
    elif period_override:
        period = period_override
    else:
        log("ERROR", company_id,
            f"{path.name} saknar #PSALDO — kan inte avgöra data-through. "
            "Ange --period YYYYMM explicit.")
        return "error"

    konto = parsed["konto"]

    # Dynamics NAV-korrigering: NAV exporterar #RES 0 som ackumulerat över
    # alla år istället för innevarande RAR. När #PROGRAM matchar NAV och
    # filen har #RES -1, subtrahera fjolåret per konto för att få korrekt YTD.
    # Detekteras strikt på "Dynamics NAV"-substring för att inte träffa andra
    # system där #RES -1 är korrekt fjolårsdata (som vi i så fall INTE ska
    # subtrahera). Verifierat 2026-05-19 mot bolag 164 — gav exakt match mot
    # Mercur-facit på samtliga testade konton.
    program = parsed.get("program") or ""
    if "Dynamics NAV" in program and parsed["res_prior"]:
        prior_by_code = dict(parsed["res_prior"])
        n_adjusted = 0
        corrected: list[tuple[str, float]] = []
        for code, amt in parsed["res"]:
            prior = prior_by_code.get(code)
            if prior is not None:
                corrected.append((code, amt - prior))
                n_adjusted += 1
            else:
                corrected.append((code, amt))
        parsed["res"] = corrected
        log("INFO", company_id,
            f"NAV-korrigering tillämpad: subtraherat #RES -1 på {n_adjusted} "
            f"konton (program='{program}')")

    # Intern konsistenskontroll (facit-fri) — körs efter NAV-korrigeringen så
    # #RES jämförs i rätt form. Loggas som WARN; blockerar inte laddningen.
    psaldo_breaks = check_psaldo_vs_res(parsed)
    if psaldo_breaks:
        worst = max(psaldo_breaks, key=lambda b: abs(b[3]))
        log("WARN", company_id,
            f"{path.name}: {len(psaldo_breaks)} konton där summa(#PSALDO) != "
            f"#RES 0 (störst: konto {worst[0]} diff {worst[3]:+.2f})")
    if include_journal:
        voucher_breaks = check_voucher_balance(parsed)
        if voucher_breaks:
            s, vnum, imb = voucher_breaks[0]
            log("WARN", company_id,
                f"{path.name}: {len(voucher_breaks)} obalanserade verifikat "
                f"(t.ex. {s}{vnum} diff {imb:+.2f})")

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
    # Klipps till <= period_override om sådan är satt (slipper skräp för framtida
    # tomma månader i filer som täcker hela FY).
    psaldo_rows: list[tuple] = []
    idx_per_period: dict[str, int] = {}
    psaldo_skipped = 0
    for p, code, amt in parsed["psaldo"]:
        if period_override and p > period_override:
            psaldo_skipped += 1
            continue
        idx_per_period[p] = idx_per_period.get(p, 0) + 1
        psaldo_rows.append(
            (p, code, konto.get(code), amt, st_for(code), idx_per_period[p])
        )
    psaldo_periods = sorted({r[0] for r in psaldo_rows})

    if not sie_rows and not psaldo_rows:
        log("WARN", company_id, f"Inga UB/RES/PSALDO-rader i {path.name}")
        return "warn"

    # Konfliktkoll: finns redan SIE/SIE_PSALDO för perioder >= filens period inom FY?
    # Bredare än bara "samma period" — fångar även scenario där en april-fil
    # har laddats tidigare och nu försöker man ladda en mars-fil ovanpå.
    fy_start, fy_end = derive_fy_range(parsed, period)
    has_override = is_override_for(override, company_id)
    existing = con.execute(
        """SELECT COUNT(*) FROM fact_balances
           WHERE company_id = %s AND source_kind IN (%s, %s)
             AND period >= %s AND period BETWEEN %s AND %s""",
        [company_id, SOURCE_KIND, SOURCE_KIND_PSALDO, period, fy_start, fy_end],
    ).fetchone()[0]
    if existing > 0 and not has_override:
        log("SKIP", company_id,
            f"{path.name}  SIE/SIE_PSALDO redan inläst för period >= {period} "
            f"inom FY {fy_start}-{fy_end} ({existing} rader). "
            "Kör med --override för att skriva över.")
        return "skip"

    total_ub = sum(r[2] for r in sie_rows if r[3] == "BS")
    total_res = sum(r[2] for r in sie_rows if r[3] == "IS")
    total = total_ub + total_res
    # SIE: UB+RES = årets resultat (YTD), inte 0. Saldobalans-check görs inte
    # här — använd verifikatnivå (fact_journal_sie) för debet/kredit-balans.
    rel_src = db.relpath_from_base(path, base_path)
    now = datetime.now()

    journal_rows: list[tuple] = []
    journal_periods: set[str] = set()
    journal_skipped = 0
    if include_journal and parsed["vouchers"]:
        journal_rows, journal_periods, journal_skipped = vouchers_to_journal_rows(
            parsed, company_id, currency, rel_src, now,
            period_cutoff=period_override,
        )

    if dry_run:
        journal_msg = (f" JOURNAL={len(journal_rows)} ({len(journal_periods)} mån)"
                       if include_journal else "")
        ovr = f"  OVERRIDE (raderar {existing} rader inom FY)" if (existing > 0 and has_override) else ""
        log("OK", company_id,
            f"[DRY] {path.name}  period={period} FY={fy_start}-{fy_end} "
            f"UB={len([r for r in sie_rows if r[3]=='BS'])} "
            f"RES={len([r for r in sie_rows if r[3]=='IS'])} "
            f"PSALDO={len(psaldo_rows)} ({len(psaldo_periods)} mån)"
            f"{journal_msg} "
            f"sum_ub={total_ub:.2f} sum_res={total_res:.2f} sum_tot={total:.2f}{ovr}")
        return "ok"

    if existing > 0 and has_override:
        log("INFO", company_id,
            f"OVERRIDE: skriver över {existing} SIE/SIE_PSALDO-rader för "
            f"period >= {period} inom FY {fy_start}-{fy_end}")

    db.sync_dim_period(con, [period] + psaldo_periods + sorted(journal_periods))

    con.execute("BEGIN")
    try:
        # Override: rensa SIE/SIE_PSALDO och journal för perioder *efter* filens
        # period inom FY (filen är "sanningen" för FY t.o.m. dess sista månad).
        # Periodens egna SIE/SIE_PSALDO/journal rensas av efterföljande
        # period-specifika DELETE nedan.
        if has_override and existing > 0:
            con.execute(
                """DELETE FROM fact_balances
                   WHERE company_id = %s AND source_kind IN (%s, %s)
                     AND period > %s AND period BETWEEN %s AND %s""",
                [company_id, SOURCE_KIND, SOURCE_KIND_PSALDO, period, fy_start, fy_end],
            )
            con.execute(
                """DELETE FROM fact_journal_sie
                   WHERE company_id = %s AND period > %s AND period BETWEEN %s AND %s""",
                [company_id, period, fy_start, fy_end],
            )
        # SIE (UB/RES): senaste laddningen vinner per (bolag, period).
        con.execute(
            """DELETE FROM fact_balances
               WHERE company_id = %s AND period = %s AND source_kind = %s""",
            [company_id, period, SOURCE_KIND],
        )
        if sie_rows:
            con.executemany(
                """INSERT INTO fact_balances
                   (company_id, period, period_type, account_code, account_name,
                    amount, currency, statement_type, source_kind, source_file,
                    row_index, loaded_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [(company_id, period, PERIOD_TYPE, r[0], r[1], r[2], currency,
                  r[3], SOURCE_KIND, rel_src, r[4], now) for r in sie_rows],
            )

        # PSALDO: senaste laddningen vinner per (bolag, period). Mars-filens
        # PSALDO för 202601 ersätter en ev. tidigare 202601-laddning från
        # samma eller annan SIE-fil.
        if psaldo_periods:
            placeholders = ",".join(["%s"] * len(psaldo_periods))
            con.execute(
                f"""DELETE FROM fact_balances
                    WHERE company_id = %s AND source_kind = %s
                    AND period IN ({placeholders})""",
                [company_id, SOURCE_KIND_PSALDO, *psaldo_periods],
            )
            con.executemany(
                """INSERT INTO fact_balances
                   (company_id, period, period_type, account_code, account_name,
                    amount, currency, statement_type, source_kind, source_file,
                    row_index, loaded_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                psaldo_fact_rows(psaldo_rows, company_id, currency, rel_src, now),
            )

        # Journal: senaste laddningen vinner per (bolag, period). En SIE-fil
        # täcker hela YTD så vouchers för 202601 från en mars-fil ersätter
        # ev. tidigare 202601-laddning från en annan SIE.
        if journal_periods:
            jp_sorted = sorted(journal_periods)
            placeholders = ",".join(["%s"] * len(jp_sorted))
            con.execute(
                f"""DELETE FROM fact_journal_sie
                    WHERE company_id = %s AND period IN ({placeholders})""",
                [company_id, *jp_sorted],
            )
            for i in range(0, len(journal_rows), JOURNAL_BATCH):
                con.executemany(
                    """INSERT INTO fact_journal_sie
                       (company_id, period, series, voucher_number, voucher_date,
                        voucher_text, line_no, account_code, account_name,
                        amount, transaction_text, quantity, currency,
                        source_file, loaded_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    journal_rows[i:i + JOURNAL_BATCH],
                )

        # SIE_VER: syntetisera YTD-saldon från verifikaten för SE/CA-bolag som
        # saknar #PSALDO. #RES-fältet är en snapshot vid genereringstiden och
        # ger skev månadsfördelning; verifikat-kumen ger exakt fördelning.
        # Vid --no-include-journal körs ingen syntes; ev. befintliga SIE_VER-
        # rader lämnas avsiktligt orörda — de speglar fortfarande den oförändrade
        # fact_journal_sie och är alltid minst lika korrekta som #RES-baserad SIE
        # (SIE_VER är en materialiserad vy av journalen).
        sie_ver_count = 0
        sie_ver_fallback = 0
        if include_journal and country in SIE_VER_COUNTRIES and not parsed["psaldo"]:
            if fy_start.endswith("01"):
                sie_ver_count, sie_ver_fallback = synthesize_sie_ver(
                    con, company_id, fy_start, fy_end, period, rel_src, now,
                    parsed_res=parsed["res"], parsed_konto=parsed["konto"])
                if sie_ver_count == 0:
                    log("INFO", company_id,
                        "SIE_VER: inga verifikat i fact_journal_sie — "
                        "behåller #RES-baserad SIE som fallback.")
                elif sie_ver_fallback > 0:
                    log("INFO", company_id,
                        f"SIE_VER: hybrid-fallback aktiverad för "
                        f"{sie_ver_fallback} konton (journal otäckande, "
                        f"#RES jämnt fördelat över FY).")
            else:
                log("WARN", company_id,
                    f"SIE_VER: brutet räkenskapsår (FY-start {fy_start}) — "
                    "hoppar över syntes (YTD-kum antar kalenderår).")
        elif country in SIE_VER_COUNTRIES and parsed["psaldo"]:
            # Bolaget levererar #PSALDO — rensa ev. stale SIE_VER från en
            # tidigare laddning då filen saknade #PSALDO. best_source föredrar
            # SIE_PSALDO så det är ofarligt numeriskt, men håll datat rent.
            # OBS: körs bara vid faktisk laddning — om konfliktkollen ovan
            # returnerat "skip" når vi aldrig hit, men då laddas heller ingen
            # ny #PSALDO så best_source ger fortsatt rätt siffror.
            con.execute(
                """DELETE FROM fact_balances
                   WHERE company_id = %s AND source_kind = %s
                     AND period BETWEEN %s AND %s""",
                [company_id, SOURCE_KIND_SIE_VER, fy_start, fy_end],
            )

        con.execute(
            """INSERT INTO load_history
               (company_id, period, source_kind, source_file, rows_loaded,
                sum_amount, statement_type_present, status, message, loaded_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [company_id, period, SOURCE_KIND, rel_src,
             len(sie_rows) + len(psaldo_rows) + len(journal_rows), total, True,
             "ok",
             f"sie_rows={len(sie_rows)} psaldo_rows={len(psaldo_rows)} "
             f"psaldo_periods={len(psaldo_periods)} "
             f"journal_rows={len(journal_rows)} journal_periods={len(journal_periods)} "
             f"sie_ver_rows={sie_ver_count} sie_ver_fallback={sie_ver_fallback} "
             f"sum_ub={total_ub:.2f} sum_res={total_res:.2f}",
             now],
        )
        con.execute("COMMIT")
    except Exception as e:
        con.execute("ROLLBACK")
        log("ERROR", company_id, f"DB-fel {path.name}: {e}")
        return "error"

    psaldo_msg = f" PSALDO={len(psaldo_rows)}({len(psaldo_periods)} mån)" if psaldo_rows else ""
    journal_msg = f" JOURNAL={len(journal_rows)}({len(journal_periods)} mån)" if journal_rows else ""
    sie_ver_msg = (f" SIE_VER={sie_ver_count}"
                   + (f"(fallback={sie_ver_fallback})" if sie_ver_fallback else "")
                   if sie_ver_count else "")
    cutoff_msg = (f"  CUTOFF<= {period_override}: skippade PSALDO={psaldo_skipped} "
                  f"vouchers={journal_skipped}"
                  if period_override and (psaldo_skipped or journal_skipped) else "")
    log("OK", company_id,
        f"{path.name}  period={period}  rader={len(sie_rows)}{psaldo_msg}{journal_msg}{sie_ver_msg}  "
        f"sum={total:.2f}{cutoff_msg}")
    return "ok"


def discover_files(source_dir: Path) -> list[Path]:
    """Hitta SIE-filer direkt i source_dir (inte i Referens/).

    Accepterar .SE/.se (vanlig) samt .SI/.si (Hogia-export). .sie hade aldrig
    fallit in i denna pipeline eftersom process_sweden.py:s output har en av
    de två första formaten, men inkluderas för robusthet."""
    if not source_dir.exists():
        return []
    return sorted(f for f in source_dir.iterdir()
                  if f.is_file() and f.suffix.upper() in {".SE", ".SI", ".SIE"})


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ladda SIE-filer (Sverige) till Postgres (fact_balances)."
    )
    parser.add_argument("--period", default=None,
                        help="YYYYMM. Override för period-validering "
                             "(default: härleds från filen)")
    parser.add_argument("--source-dir", default=None,
                        help="Mapp att läsa från "
                             "(default: extracted/{period}/Sweden under base_path)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--include-journal", default=True,
                        action=argparse.BooleanOptionalAction,
                        help="Ladda även #VER/#TRANS till fact_journal_sie. "
                             "Default: aktivt. --no-include-journal stänger av "
                             "(kan vara tungt för stora filer).")
    parser.add_argument("--override", nargs="*", type=int, default=None, metavar="ID",
                        help="Skriv över befintlig SIE/SIE_PSALDO inom FY. "
                             "--override = global; --override 134 196 = bara dessa bolag.")
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
        log("WARN", "scan", f"Inga SIE-filer (.SE/.SI/.SIE) hittades i {source_dir}")
        log("DONE", "load_sie.py", "0 OK  0 WARN  0 SKIP  0 ERROR")
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
            log("ERROR", "scan",
                "Inga bolag med orgnr i dim_company — kör 'py db.py' först")
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

    log("DONE", "load_sie.py",
        f"{counts['ok']} OK  {counts['warn']} WARN  "
        f"{counts['skip']} SKIP  {counts['error']} ERROR")


if __name__ == "__main__":
    main()
