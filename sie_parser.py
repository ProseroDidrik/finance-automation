"""Kanonisk SIE-parsning (typ 4) — delad mellan load_sie.py och process_sweden.py.

Ren parse- och valideringslogik: ingen databas, inga loader-specifika rad-
former. SIE-filer är CP437-kodade (PC8 enligt #FORMAT); se read_text_with_fallback.

Tidigare fanns två separata parse_sie-implementationer (load_sie.py + den enklare
i process_sweden.py). Den här modulen är enda källan.
"""
from __future__ import annotations

import re
from pathlib import Path

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


def normalize_orgnr(orgnr: str) -> str:
    """Strip allt utom siffror — '556071-2340' → '5560712340'."""
    return re.sub(r"[^0-9]", "", str(orgnr).strip())


def read_text_with_fallback(path: Path) -> str:
    """Läs SIE-fil med encoding-fallback.

    SIE-standarden anger PC8 = IBM CP437 (deklareras i #FORMAT). Vi provar ändå
    utf-8-sig först eftersom enstaka nyare exporter skriver UTF-8, och latin-1
    som sista skydd för Windows-1252-varianter."""
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


def derive_period(parsed: dict) -> str | None:
    """Endast #PSALDO är ett tillförlitligt 'data-through'-signal i SIE.

    #GEN är exportdatum (kan vara senare än datat) och #RAR är FY-slut
    (alltid YYYY1231 för månadsfiler). Båda är därför värdelösa för att
    avgöra vilken period datat representerar.
    """
    if parsed["psaldo"]:
        return max(p for p, _, _ in parsed["psaldo"])
    return None


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

    Summan av #TRANS-belopp i ett #VER ska vara 0 (SIE 4B, #TRANS regel 4).
    Returnerar list[(series, voucher_number, imbalance)] för verifikat vars
    |summa| > tol; tom lista = alla verifikat balanserar. Kräver
    parse_sie(..., with_journal=True).
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
