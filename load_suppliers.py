"""Ladda leverantörsstatistik (spend per leverantör × bolag × år) till
fact_supplier_spend + dim_supplier_register i DuckDB.

Källa: <base_path>/_statistics/Supplier/
  - _Master leverantör Sverige.xlsx   (okrypterad)

Schema:
  - dim_supplier_register: register över levprefix → (supplier_name, kategori, segment)
  - fact_supplier_spend:   en rad per (bolag, lev_nr, namn, year, period_kind)

Idempotens: alla rader för ett land tas bort innan ny laddning. Re-run skriver om allt.

CLI:
    py load_suppliers.py                      # alla länder med fil (just nu bara Sverige)
    py load_suppliers.py --country Sweden
    py load_suppliers.py --dry-run            # parsa + rapportera, skriv inget
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path

import pandas as pd

import db
from shared import begin_run, load_config, log

SUPPLIER_DIR = "_statistics/Supplier"

FILES = {
    "Sweden": "_Master leverantör Sverige.xlsx",
}

# Bolag-strängen i Excel "Data"-fliken → company_id i dim_company.
# Verifierad mot Insamling-fliken (orgnr) och dim_company.
SE_BOLAG_TO_ID: dict[str, int] = {
    "Alexandersson":     14,    # Låssmeden Sven Alexandersson
    "All Round":         97,    # All-Round Låsservice
    "Axel Group":        32,
    "Axlås":              1,    # Axlås Solidlås
    "CFS":               75,    # CFS Larm
    "Citylåset":         10,    # Citylåset i Kristianstad
    "Creab":            105,    # Creab Säkerhet
    "Dala Lås":          72,
    "Dala Lås Ludvika":  73,
    "Dalek":             70,    # Dalek Lås & Larm
    "El & Fast":        164,    # El & Fastighetsdrift Stockholm
    "Exista":           151,    # Exista Säkerhet
    "Falu":              74,    # Falu Lås & Nyckelservice
    "Farsta":             3,    # Farsta Lås
    "Gävle":             41,    # Lås & Nyckel i Gävle
    "Haninge":           89,    # Haninge Lås
    "Hässleholm":        93,    # Hässleholms Låssmed
    "Kanlås":           186,    # Låssmeden Kanlås
    "Kungälv":          240,    # Kungälvs Lås
    "LH Alarm":          11,    # LH Electronic Alarm
    "Larmatic":         110,    # Larmatic Alarm
    "Lås Arne":         197,    # Lås-Arne Malmström
    "Låskomfort":        88,
    "Montageservice":    94,    # Montageservice i Kalmar
    "Norrbotten":       172,    # Norrbottens Larmkonsult
    "Norrköping":       102,    # Låssmeden i Norrköping
    "Norrskydd":         76,    # AB Norrskydd
    "OpenUP":            12,    # alias
    "OpenUp":            12,
    "Passera":            4,    # Passéra
    "Rikstvåan":         18,    # Rikstvåans Låsservice
    "Safeexit":         222,
    "Safetytech":        23,    # Safetytech i Väst
    "Samuelsson":        82,    # Samuelsson & Partner
    "Sickla":            87,    # Sickla Låsteknik
    "Skara":              6,    # Tele & Säkerhetstjänst i Skara
    "Sundsvall":         86,    # Sundsvalls El och Larmservice
    "Swedsecur":         15,
    "Säkerhetspartner": 239,    # Säkerhetspartner i väst
    "Säkerhetsteknik":   33,    # Säkerhetsteknik i Örestad
    "Södra Vägen":      152,    # Södra Vägens Låsservice
    "Telos":              5,    # Telos Telemontage
    "UST":              180,    # Uppsala Säkerhetsteknik
    "Zenita":             7,
    # Förekommer i Levregister men ej i Data: Doorway → 162 (Prosero Doorway).
    "Doorway":          162,
}

# År-kolumner i "Data"-fliken → (year, period_kind).
# Notera: '2021-2023' är en pivot-rollup (= 2021+2022+2023) och ska skippas.
YEAR_COLUMNS: list[tuple[str, int, str]] = [
    ("2021",    2021, "FULL"),
    ("2022",    2022, "FULL"),
    ("2023",    2023, "FULL"),
    ("2024",    2024, "FULL"),
    ("2025",    2025, "FULL"),
    ("2024 H1", 2024, "H1"),
    ("2025 H1", 2025, "H1"),
]


def _to_str(v) -> str | None:
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    return s if s else None


def _clean_class(v) -> str | None:
    """0 / 0.0 / tom sträng → None. Annars trimmad sträng."""
    s = _to_str(v)
    if s is None:
        return None
    if s in ("0", "0.0"):
        return None
    return s


def parse_levregister(path: Path) -> dict[str, dict]:
    """Returnera {levprefix → {supplier_name, kategori, segment}}.

    Vid dubletter: föredra raden med mest icke-tomma kategori/segment.
    """
    df = pd.read_excel(path, sheet_name="Levregister", engine="openpyxl")
    df.columns = [str(c).strip() for c in df.columns]
    out: dict[str, dict] = {}
    for _, r in df.iterrows():
        lp = _to_str(r.get("Levprefix"))
        if not lp:
            continue
        rec = {
            "supplier_name": _to_str(r.get("Leverantör")),
            "kategori":      _clean_class(r.get("Kategori")),
            "segment":       _clean_class(r.get("Segment")),
        }
        existing = out.get(lp)
        if existing is None:
            out[lp] = rec
            continue
        # Dedupe: föredra raden med flest ifyllda fält
        score_new = sum(v is not None for v in rec.values())
        score_old = sum(v is not None for v in existing.values())
        if score_new > score_old:
            out[lp] = rec
    return out


def parse_data(
    path: Path, bolag_to_id: dict[str, int], currency: str,
) -> tuple[list[dict], list[tuple]]:
    """Returnera (rader, ignored). Rader är fact_supplier_spend-records.

    Snapshotar Leverantör/Kategori/Segment direkt från Data-fliken (där Excel
    redan har gjort VLOOKUP mot Levregister) — så att pivots inte beror på
    en register-join som kan vara ofullständig.
    """
    df = pd.read_excel(path, sheet_name="Data", engine="openpyxl")
    df.columns = [str(c).strip() for c in df.columns]
    for col, _, _ in YEAR_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    rows: list[dict] = []
    ignored: list[tuple] = []
    seen_unmapped: set[str] = set()

    for _, r in df.iterrows():
        bolag = _to_str(r.get("Bolag"))
        if not bolag:
            continue
        cid = bolag_to_id.get(bolag)
        if cid is None:
            if bolag not in seen_unmapped:
                ignored.append(("unmapped_bolag", bolag))
                seen_unmapped.add(bolag)
            continue
        lev_nr = _to_str(r.get("Lev nr"))
        # Lev nr kan saknas — vi behåller raden ändå (Excel-pivots gör detsamma).
        namn = _to_str(r.get("Namn"))
        levprefix = _to_str(r.get("Levprefix"))
        supplier_name = _to_str(r.get("Leverantör"))
        kategori = _clean_class(r.get("Kategori"))
        segment = _clean_class(r.get("Segment"))

        for col, year, kind in YEAR_COLUMNS:
            amt = r.get(col)
            if amt is None or pd.isna(amt):
                continue
            try:
                amt_f = float(amt)
            except (TypeError, ValueError):
                continue
            if amt_f == 0:
                continue
            rows.append({
                "company_id":    cid,
                "bolag_label":   bolag,
                "lev_nr":        lev_nr,
                "namn":          namn,
                "levprefix":     levprefix,
                "supplier_name": supplier_name,
                "kategori":      kategori,
                "segment":       segment,
                "year":          year,
                "period_kind":   kind,
                "amount":        amt_f,
                "currency":      currency,
            })
    return rows, ignored


# ---------------------------------------------------------------------------
# Skrivning till DuckDB
# ---------------------------------------------------------------------------

INSERT_REG_SQL = """
INSERT INTO dim_supplier_register
    (country, levprefix, supplier_name, kategori, segment, source_file, loaded_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
"""

INSERT_FACT_SQL = """
INSERT INTO fact_supplier_spend
    (country, company_id, bolag_label, lev_nr, namn, levprefix,
     supplier_name, kategori, segment,
     year, period_kind, amount, currency, source_file, loaded_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def write_country(
    con, country: str,
    register: dict[str, dict],
    facts: list[dict],
    source_file: str,
) -> None:
    now = datetime.now()
    reg_payload = [
        (country, lp, rec["supplier_name"], rec["kategori"], rec["segment"],
         source_file, now)
        for lp, rec in register.items()
    ]
    fact_payload = [
        (country, r["company_id"], r["bolag_label"], r["lev_nr"], r["namn"],
         r["levprefix"], r["supplier_name"], r["kategori"], r["segment"],
         r["year"], r["period_kind"], r["amount"],
         r["currency"], source_file, now)
        for r in facts
    ]
    con.execute("BEGIN")
    try:
        con.execute("DELETE FROM dim_supplier_register WHERE country = ?", [country])
        con.execute("DELETE FROM fact_supplier_spend  WHERE country = ?", [country])
        if reg_payload:
            con.executemany(INSERT_REG_SQL, reg_payload)
        if fact_payload:
            con.executemany(INSERT_FACT_SQL, fact_payload)
        con.execute(
            """INSERT INTO load_history
               (company_id, period, source_kind, source_file, rows_loaded, sum_amount,
                statement_type_present, status, message, loaded_at)
               VALUES (NULL, NULL, 'SUPPLIER', ?, ?, ?, FALSE, 'ok', ?, ?)""",
            [source_file, len(fact_payload),
             sum(r["amount"] for r in facts) if facts else 0.0,
             f"country={country} register_rows={len(reg_payload)}", now],
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# Pivot-sanity-check (Sverige)
# ---------------------------------------------------------------------------

def verify_sweden_pivot(con, source_path: Path) -> None:
    """Verifiera att fact_supplier_spend ger samma 2024-summa per Leverantör/
    Kategori som direktaggregering ur Excel Data-fliken.

    Notera: Excel-flikarna 'Summering' / 'Summering (2)' är pre-beräknade
    pivots som kan vara stale. Vi jämför istället mot Data-fliken som är
    den autoritativa råkällan.
    """
    df = pd.read_excel(source_path, sheet_name="Data", engine="openpyxl")
    df.columns = [str(c).strip() for c in df.columns]
    df["2024"] = pd.to_numeric(df["2024"], errors="coerce")
    # Begränsa till bolag vi laddar (ignorera ev. unmapped Bolag-strängar)
    df = df[df["Bolag"].isin(SE_BOLAG_TO_ID.keys())]

    expected_supplier = (
        df.dropna(subset=["Leverantör", "2024"])
          .groupby("Leverantör")["2024"].sum()
          .sort_values(ascending=False).head(10).to_dict()
    )
    expected_kat = (
        df[df["Kategori"].notna() & ~df["Kategori"].astype(str).str.strip().isin(["0","0.0",""])]
          .dropna(subset=["2024"])
          .groupby("Kategori")["2024"].sum()
          .sort_values(ascending=False).head(10).to_dict()
    )

    sup_rows = con.execute(
        """SELECT supplier_name, SUM(amount)
           FROM fact_supplier_spend
           WHERE country='Sweden' AND year=2024 AND period_kind='FULL'
             AND supplier_name IS NOT NULL
           GROUP BY supplier_name""",
    ).fetchall()
    by_sup = {r[0]: r[1] for r in sup_rows}

    kat_rows = con.execute(
        """SELECT kategori, SUM(amount)
           FROM fact_supplier_spend
           WHERE country='Sweden' AND year=2024 AND period_kind='FULL'
             AND kategori IS NOT NULL
           GROUP BY kategori""",
    ).fetchall()
    by_kat = {r[0]: r[1] for r in kat_rows}

    tol = 1.0
    for name, expected in expected_supplier.items():
        got = by_sup.get(name)
        if got is None or abs(got - expected) > tol:
            raise AssertionError(
                f"Pivot-check FEL leverantör {name!r} 2024: DB={got}, Excel={expected}"
            )
    for cat, expected in expected_kat.items():
        got = by_kat.get(cat)
        if got is None or abs(got - expected) > tol:
            raise AssertionError(
                f"Pivot-check FEL kategori {cat!r} 2024: DB={got}, Excel={expected}"
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(country_filter: str | None, dry_run: bool) -> int:
    config = load_config()
    base_path = Path(config["base_path"])
    period = datetime.now().strftime("%Y%m")
    begin_run("load_suppliers", period)
    log("START", "load_suppliers.py",
        f"period {period}{' [DRY RUN]' if dry_run else ''}")

    con = db.connect()
    try:
        valid_ids = {
            r[0] for r in con.execute("SELECT company_id FROM dim_company").fetchall()
        }

        # Hämta valuta från första bolag i landet
        countries = [country_filter] if country_filter else list(FILES.keys())
        ok_count = warn_count = err_count = 0

        for country in countries:
            if country not in FILES:
                log("ERROR", country, "okänt land")
                err_count += 1
                continue
            f = base_path / SUPPLIER_DIR / FILES[country]
            if not f.exists():
                log("ERROR", country, f"filen saknas: {f}")
                err_count += 1
                continue

            currency = db.COUNTRY_CURRENCY.get(country, "")
            if not currency:
                log("ERROR", country, f"saknad valuta-mappning för {country}")
                err_count += 1
                continue

            if country == "Sweden":
                bolag_to_id = SE_BOLAG_TO_ID
            else:
                log("ERROR", country, "ingen Bolag→id-mappning konfigurerad")
                err_count += 1
                continue

            # Filtrera mappingen till verifierade IDs
            invalid = [b for b, cid in bolag_to_id.items() if cid not in valid_ids]
            if invalid:
                log("WARN", country, f"bolag i mappingen saknas i dim_company: {invalid}")

            try:
                register = parse_levregister(f)
                facts, ignored = parse_data(f, bolag_to_id, currency)
            except Exception as e:
                log("ERROR", country, f"parsning misslyckades: {e}")
                err_count += 1
                continue

            for reason, *info in ignored[:10]:
                log("WARN", country, f"hoppade ({reason}): {info}")
            if len(ignored) > 10:
                log("WARN", country, f"... + {len(ignored) - 10} fler ignorerade")

            n_companies = len({r["company_id"] for r in facts})
            n_suppliers = len({(r["bolag_label"], r["lev_nr"]) for r in facts})
            msg = (f"register={len(register)} levprefix, "
                   f"fact={len(facts)} rader, "
                   f"{n_suppliers} unika leverantörer, "
                   f"{n_companies} bolag")
            if ignored:
                msg += f", {len(ignored)} ignorerade Bolag-strängar"

            if dry_run:
                log("INFO", country, msg + " [DRY — skriver inget]")
            else:
                source_file = db.relpath_from_base(f, base_path)
                write_country(con, country, register, facts, source_file)
                log("OK", country, msg)
                ok_count += 1
                if ignored:
                    warn_count += 1

        if not dry_run and country_filter in (None, "Sweden") and ok_count > 0:
            try:
                se_path = base_path / SUPPLIER_DIR / FILES["Sweden"]
                verify_sweden_pivot(con, se_path)
                log("INFO", "Sweden",
                    "Pivot-check OK (top-10 leverantör + top-10 kategori 2024 matchar Excel Data-fliken)")
            except AssertionError as e:
                log("ERROR", "Sweden", str(e))
                err_count += 1
                return 2

        log("DONE", "load_suppliers.py",
            f"{ok_count} OK  {warn_count} WARN  {err_count} ERROR")
        return 0 if err_count == 0 else 1
    finally:
        con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--country", choices=list(FILES.keys()),
                    help="bara ett land (default: alla med konfigurerad fil)")
    ap.add_argument("--dry-run", action="store_true",
                    help="parsa + rapportera, skriv inget till databasen")
    args = ap.parse_args()
    raise SystemExit(run(args.country, args.dry_run))


if __name__ == "__main__":
    main()
