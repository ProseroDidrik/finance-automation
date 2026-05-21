"""Ladda personalstatistik (FTE) per land till fact_personnel i DuckDB.

Källa: <base_path>/_statistics/FTE/
  - Personal - master Sverige.xlsx   (krypterad — config.personnel_password)
  - Personel Norway.xlsx             (krypterad — config.personnel_password)
  - Combined Personnel Finland.xlsx  (okrypterad)

Idempotens: alla rader för ett land tas bort innan ny laddning. Re-run skriver om allt.

CLI:
    py load_personnel.py                      # alla tre länder
    py load_personnel.py --country Sweden     # bara ett land (Sweden|Norway|Finland)
    py load_personnel.py --dry-run            # parsa + rapportera, skriv inget
"""
from __future__ import annotations

import argparse
import io
from datetime import date, datetime
from pathlib import Path

import msoffcrypto
import pandas as pd

import db
from shared import begin_run, load_config, log

# ---------------------------------------------------------------------------
# Konfig
# ---------------------------------------------------------------------------

FTE_DIR = "_statistics/FTE"
FILES = {
    "Sweden":  ("Personal - master Sverige.xlsx",   True,  "Data",        "A:Q"),
    "Norway":  ("Personel Norway.xlsx",             True,  "Data",        None),
    "Finland": ("Combined Personnel Finland.xlsx",  False, "Combination", None),
}

# Finland: Yritys → company_id (manuellt verifierat mot dim_company och Excel-pivoten).
# 25 distinkta värden i datat → 20 unika bolag (några har stavningsvariationer).
FI_NAME_TO_ID: dict[str, int] = {
    "Lukkoluket OY":                       177,
    "Lukkoluket Oy":                       177,
    "LukkoLuket Oy":                       177,
    "PAP Group Oy":                        170,
    "Arvolukko Oy":                        134,
    "Avain-Asema Oy":                      146,
    "Meri-Lapin Lukituspalvelu Oy":        195,
    "Meri-Lapin Lukituspalvelu OY":        195,
    "THV Tele-ja Hälytysvalvonta Oy":      182,
    "Turvatalo - Tapiolan Yleishuolto Oy": 153,
    "Ajan Lukko Oy":                       179,
    "Tele-Projekti Oy":                    181,   # trailing space tas bort av .strip()
    "tele-Projekti Oy":                    181,
    "Suomen Turvalukko Oy":                185,
    "Lukitustekniikka-STY Oy":             161,
    "Avainahjo Oy":                        173,
    "Jm- Lukko ja Turvatekniikka Oy":      221,
    "JM Lukko- ja Turvatekniikka Oy":      221,
    "Emsec Oy":                            215,
    "ST Hälytys Oy":                       196,
    "ANV Lukkopalvelu Oy":                 238,
    "Hyvinkään Turvalukko Oy":             223,
    "Lukkoässä Oy":                        166,
    "Etelä-Suomen Hälytintekniikka Oy":    199,
    "Suomen Turvakonsultit Oy":            193,
}

# ---------------------------------------------------------------------------
# Hjälpfunktioner
# ---------------------------------------------------------------------------

def _open_excel(path: Path, encrypted: bool, password: str | None) -> io.BytesIO | Path:
    """Returnerar en BytesIO (dekrypterad) eller filsökvägen direkt."""
    if not encrypted:
        return path
    if not password:
        raise RuntimeError(
            f"{path.name}: filen är krypterad men personnel_password saknas i config.json"
        )
    with open(path, "rb") as fp:
        ofile = msoffcrypto.OfficeFile(fp)
        ofile.load_key(password=password)
        buf = io.BytesIO()
        ofile.decrypt(buf)
    buf.seek(0)
    return buf


def _to_date(v) -> date | None:
    """Konvertera olika datumrepresentationer → date eller None.

    pd.NaT fångas explicit via pd.isna: NaT är en datetime-subklass och skulle
    annars smita förbi isinstance-kollen nedan och returneras som NaT i stället
    för None (samma mönster som _to_str använder).
    """
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, pd.Timestamp):
        return v.date()
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s or s == "-":
            return None
        try:
            ts = pd.to_datetime(s, errors="coerce")
        except Exception:
            return None
        return None if pd.isna(ts) else ts.date()
    return None


def _to_float(v) -> float | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", ".")
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _to_str(v) -> str | None:
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    return s if s and s != "-" else None


def _norm_gender(v) -> str | None:
    s = _to_str(v)
    if s is None:
        return None
    s_low = s.lower()
    if s_low in ("m", "man", "male", "mies"):
        return "M"
    if s_low in ("f", "k", "kvinna", "female", "nainen"):
        return "F"
    return None


def _se_birth_date(v) -> date | None:
    """Sverige: '041126-xxxx' eller '871013-4894' → date.

    De första 6 tecknen är YYMMDD. Sekel: ≤25 → 20xx, annars 19xx.
    Returnerar None om strängen inte matchar.
    """
    s = _to_str(v)
    if s is None or len(s) < 6 or not s[:6].isdigit():
        return None
    yy, mm, dd = int(s[:2]), int(s[2:4]), int(s[4:6])
    year = 2000 + yy if yy <= 25 else 1900 + yy
    try:
        return date(year, mm, dd)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Parsers per land — returnerar list[dict] med fält som matchar fact_personnel
# ---------------------------------------------------------------------------

def parse_sweden(buf, valid_ids: set[int]) -> tuple[list[dict], list[tuple]]:
    """Returnera (rader, ignored). ignored = [(reason, identifier, ...)]."""
    df = pd.read_excel(buf, sheet_name="Data", engine="openpyxl", usecols="A:Q")
    df.columns = [
        "Land", "ID", "NR", "Bolag", "Namn", "Titel", "Fodelse",
        "Anstallning", "Slut", "Avg", "Anst", "Prod", "MK", "Kat",
        "ArBorjat", "ArSlutat", "PenLar",
    ]
    rows, ignored = [], []
    for _, r in df.iterrows():
        cid_raw = r["ID"]
        if pd.isna(cid_raw):
            continue
        try:
            cid = int(cid_raw)
        except (TypeError, ValueError):
            ignored.append(("bad_id", cid_raw, r.get("Bolag"), r.get("Namn")))
            continue
        name = _to_str(r["Namn"])
        if not name:
            continue
        if cid not in valid_ids:
            ignored.append(("unknown_company", cid, r.get("Bolag"), name))
            continue
        rows.append({
            "company_id":         cid,
            "employee_name":      name,
            "title":              _to_str(r["Titel"]),
            "birth_date":         _se_birth_date(r["Fodelse"]),
            "employed_from":      _to_date(r["Anstallning"]),
            "employed_to":        _to_date(r["Slut"]),
            "termination_reason": _to_str(r["Avg"]),
            "employment_pct":     _to_float(r["Anst"]),
            "productivity":       _to_float(r["Prod"]),
            "billable_pct":       None,
            "gender":             _norm_gender(r["MK"]),
            "category":           _to_str(r["Kat"]),
            "salary_local":       None,
            "location":           None,
            "apprenticeship_end": None,
            "pension_apprentice": _to_str(r["PenLar"]),
        })
    return rows, ignored


def parse_norway(buf, valid_ids: set[int]) -> tuple[list[dict], list[tuple]]:
    df = pd.read_excel(buf, sheet_name="Data", engine="openpyxl")
    # Kolumner enligt fil: 'Country', 'Company ID Mercur', 'Company', 'Name', 'Title',
    # 'Date of birth', 'Date of employment', 'End date of employment',
    # 'Reason for termination of employment', '% of employment', 'Male/Female',
    # 'Category', 'Produktivity', 'Working at present',
    # 'Location\n(if relevant)', 'Apprentice-ship end date'
    rows, ignored = [], []
    for _, r in df.iterrows():
        cid_raw = r.get("Company ID Mercur")
        if pd.isna(cid_raw):
            continue
        try:
            cid = int(cid_raw)
        except (TypeError, ValueError):
            ignored.append(("bad_id", cid_raw, r.get("Company"), r.get("Name")))
            continue
        name = _to_str(r.get("Name"))
        if not name:
            continue
        if cid not in valid_ids:
            ignored.append(("unknown_company", cid, r.get("Company"), name))
            continue
        rows.append({
            "company_id":         cid,
            "employee_name":      name,
            "title":              _to_str(r.get("Title")),
            "birth_date":         _to_date(r.get("Date of birth")),
            "employed_from":      _to_date(r.get("Date of employment")),
            "employed_to":        _to_date(r.get("End date of employment")),
            "termination_reason": _to_str(r.get("Reason for termination of employment")),
            "employment_pct":     _to_float(r.get("% of employment")),
            "productivity":       _to_float(r.get("Produktivity")),
            "billable_pct":       None,
            "gender":             _norm_gender(r.get("Male/Female")),
            "category":           _to_str(r.get("Category")),
            "salary_local":       None,
            "location":           _to_str(r.get("Location\n(if relevant)")),
            "apprenticeship_end": _to_date(r.get("Apprentice-ship end date")),
            "pension_apprentice": None,
        })
    return rows, ignored


def parse_finland(buf, valid_ids: set[int]) -> tuple[list[dict], list[tuple]]:
    df = pd.read_excel(buf, sheet_name="Combination", engine="openpyxl")
    # Kolumner: 'Yritys', 'Työntekijän nimi', 'Positio', 'Syntymäaika', 'aloituspvm',
    # 'Lopetuspvm', 'syy lähtöön', '% työaika', 'Laskutettavaa työtä', 'Mies/Nainen',
    # 'Palkka', 'Palkka korjattu'
    rows, ignored = [], []
    for _, r in df.iterrows():
        yritys = _to_str(r.get("Yritys"))
        if not yritys:
            continue
        cid = FI_NAME_TO_ID.get(yritys)
        if cid is None:
            ignored.append(("unmapped_yritys", yritys, r.get("Työntekijän nimi")))
            continue
        if cid not in valid_ids:
            ignored.append(("unknown_company", cid, yritys, r.get("Työntekijän nimi")))
            continue
        name = _to_str(r.get("Työntekijän nimi"))
        if not name:
            continue
        # Salary: föredra korrigerad om numerisk, annars råvärdet om numeriskt
        salary = _to_float(r.get("Palkka korjattu"))
        if salary is None:
            salary = _to_float(r.get("Palkka"))
        rows.append({
            "company_id":         cid,
            "employee_name":      name,
            "title":              _to_str(r.get("Positio")),
            "birth_date":         _to_date(r.get("Syntymäaika")),
            "employed_from":      _to_date(r.get("aloituspvm")),
            "employed_to":        _to_date(r.get("Lopetuspvm")),
            "termination_reason": _to_str(r.get("syy lähtöön")),
            "employment_pct":     _to_float(r.get("% työaika")),
            "productivity":       None,
            "billable_pct":       _to_float(r.get("Laskutettavaa työtä")),
            "gender":             _norm_gender(r.get("Mies/Nainen")),
            "category":           None,
            "salary_local":       salary,
            "location":           None,
            "apprenticeship_end": None,
            "pension_apprentice": None,
        })
    return rows, ignored


PARSERS = {"Sweden": parse_sweden, "Norway": parse_norway, "Finland": parse_finland}


# ---------------------------------------------------------------------------
# Skrivning till DuckDB
# ---------------------------------------------------------------------------

INSERT_SQL = """
INSERT INTO fact_personnel (
    country, company_id, employee_name, title, birth_date,
    employed_from, employed_to, termination_reason,
    employment_pct, productivity, billable_pct,
    gender, category, salary_local,
    location, apprenticeship_end, pension_apprentice,
    snapshot_date, source_file, loaded_at
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def write_country(
    con, country: str, rows: list[dict],
    source_file: str, snapshot_date: date,
) -> None:
    """Idempotent skrivning för ett land. En transaktion."""
    now = datetime.now()
    payload = [
        (
            country, r["company_id"], r["employee_name"], r["title"], r["birth_date"],
            r["employed_from"], r["employed_to"], r["termination_reason"],
            r["employment_pct"], r["productivity"], r["billable_pct"],
            r["gender"], r["category"], r["salary_local"],
            r["location"], r["apprenticeship_end"], r["pension_apprentice"],
            snapshot_date, source_file, now,
        )
        for r in rows
    ]
    con.execute("BEGIN")
    try:
        con.execute("DELETE FROM fact_personnel WHERE country = %s", [country])
        if payload:
            con.executemany(INSERT_SQL, payload)
        con.execute(
            """INSERT INTO load_history
               (company_id, period, source_kind, source_file, rows_loaded, sum_amount,
                statement_type_present, status, message, loaded_at)
               VALUES (NULL, %s, 'PERSONNEL', %s, %s, NULL, FALSE, 'ok', %s, %s)""",
            [snapshot_date.strftime("%Y%m"), source_file, len(payload),
             f"country={country}", now],
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# Pivot-sanity-check (Sverige) — säkerställer att laddningen ger samma siffror
# som Excel-filens 'Anställda tabell' för Passera (company_id=160).
# ---------------------------------------------------------------------------

PIVOT_EXPECTED_PASSERA = {
    2023: {"ub": 117},
    2024: {"ub": 97,  "began": 5,  "slutat": 25},
    2025: {"ub": 89,  "began": 3,  "slutat": 11},
    2026: {"ub": 92,  "began": 4,  "slutat": 1},
}


def verify_sweden_pivot(con) -> None:
    """Räknar UB/Began/Slutat ur fact_personnel för Passera och jämför med pivoten."""
    for year, expected in PIVOT_EXPECTED_PASSERA.items():
        end = date(year, 12, 31)
        ub = con.execute(
            """SELECT COUNT(*) FROM fact_personnel
               WHERE country='Sweden' AND company_id=160
                 AND employed_from <= %s
                 AND (employed_to IS NULL OR employed_to > %s)""",
            [end, end],
        ).fetchone()[0]
        if ub != expected["ub"]:
            raise AssertionError(
                f"Pivot-check FEL Passera {year}: UB={ub}, väntat {expected['ub']}"
            )
        if "began" in expected:
            began = con.execute(
                """SELECT COUNT(*) FROM fact_personnel
                   WHERE country='Sweden' AND company_id=160
                     AND EXTRACT(year FROM employed_from) = %s""",
                [year],
            ).fetchone()[0]
            slutat = con.execute(
                """SELECT COUNT(*) FROM fact_personnel
                   WHERE country='Sweden' AND company_id=160
                     AND EXTRACT(year FROM employed_to) = %s""",
                [year],
            ).fetchone()[0]
            if began != expected["began"] or slutat != expected["slutat"]:
                raise AssertionError(
                    f"Pivot-check FEL Passera {year}: "
                    f"began={began} (väntat {expected['began']}), "
                    f"slutat={slutat} (väntat {expected['slutat']})"
                )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(country_filter: str | None, dry_run: bool) -> int:
    config = load_config()
    base_path = Path(config["base_path"])
    password = config.get("personnel_password")
    period = datetime.now().strftime("%Y%m")
    begin_run("load_personnel", period)
    log("START", "load_personnel.py",
        f"period {period}{' [DRY RUN]' if dry_run else ''}")

    con = db.connect()
    try:
        valid_ids = {
            r[0] for r in con.execute(
                "SELECT company_id FROM dim_company"
            ).fetchall()
        }

        countries = [country_filter] if country_filter else list(FILES.keys())
        warn_count = ok_count = err_count = 0

        for country in countries:
            if country not in FILES:
                log("ERROR", country, "okänt land")
                err_count += 1
                continue
            filename, encrypted, _, _ = FILES[country]
            f = base_path / FTE_DIR / filename
            if not f.exists():
                log("ERROR", country, f"filen saknas: {f}")
                err_count += 1
                continue

            try:
                buf = _open_excel(f, encrypted, password)
                rows, ignored = PARSERS[country](buf, valid_ids)
            except Exception as e:
                log("ERROR", country, f"parsning misslyckades: {e}")
                err_count += 1
                continue

            for reason, *info in ignored[:5]:
                log("WARN", country, f"hoppade ({reason}): {info}")
            if len(ignored) > 5:
                log("WARN", country, f"... + {len(ignored) - 5} fler ignorerade rader")

            n_companies = len({r["company_id"] for r in rows})
            msg = f"{len(rows)} rader, {n_companies} bolag"
            if ignored:
                msg += f", {len(ignored)} ignorerade"

            if dry_run:
                log("INFO", country, msg + " [DRY — skriver inget]")
            else:
                snapshot_date = date.fromtimestamp(f.stat().st_mtime)
                source_file = db.relpath_from_base(f, base_path)
                write_country(con, country, rows, source_file, snapshot_date)
                log("OK", country, msg + f"  snapshot={snapshot_date}")
                ok_count += 1
                if ignored:
                    warn_count += 1

        if not dry_run and country_filter in (None, "Sweden"):
            try:
                verify_sweden_pivot(con)
                log("INFO", "Sweden", "Pivot-check Passera (id=160) OK")
            except AssertionError as e:
                log("ERROR", "Sweden", str(e))
                err_count += 1
                return 2

        log("DONE", "load_personnel.py",
            f"{ok_count} OK  {warn_count} WARN  {err_count} ERROR")
        return 0 if err_count == 0 else 1
    finally:
        con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--country", choices=list(FILES.keys()),
                    help="bara ett land (default: alla tre)")
    ap.add_argument("--dry-run", action="store_true",
                    help="parsa + rapportera, skriv inget till databasen")
    args = ap.parse_args()
    raise SystemExit(run(args.country, args.dry_run))


if __name__ == "__main__":
    main()
