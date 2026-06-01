"""build.py — CLI som bygger YTD-nyckeltalsdashboarden (HTML + Excel) ur warehouse.

Bryter ut Cowork-bygg-iterationerna till commit:bar repo-kod. Eva-rebuild = ett
kommando:

    py dashboards/ytd_nyckeltal/build.py --period 202604 \
        --facit-dir "<...>/mercur_facit" --output ./tmp/v14/

Flöde (se README): queries → aggregate → dash → (validate) → render HTML + xlsx.
Data-lagret verifieras mot ett känt ankare (koncern Total Sales) INNAN rendering,
så query/FX/best_source-buggar isoleras från renderar-buggar.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import db_io  # noqa: E402
from aggregate import build_dashboard_data  # noqa: E402
from config import (  # noqa: E402
    DEFAULT_PERIOD, EXPECTED_KONCERN_SALES_202604_MSEK, KONCERN_SALES_TOLERANCE,
)


def log(status: str, msg: str) -> None:
    print(f"[{status:<5}] {msg}")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Bygg YTD-nyckeltalsdashboarden.")
    ap.add_argument("--period", default=DEFAULT_PERIOD, help="YTD-period YYYYMM (default 202604)")
    ap.add_argument("--facit-dir", type=Path, default=None,
                    help="Mapp med Mercur Resultaträkning (20).xlsx m.fl. Utelämna = ingen validering.")
    ap.add_argument("--output", type=Path, default=Path("./tmp/v14"),
                    help="Output-mapp för Nyckeltal.html + .xlsx")
    ap.add_argument("--no-validate", action="store_true",
                    help="Hoppa över Mercur-validering även om --facit-dir anges.")
    ap.add_argument("--data-only", action="store_true",
                    help="Bygg + verifiera datalagret, skriv dashboard_data.json, rendera inget.")
    return ap.parse_args(argv)


def koncern_sales_msek(dash: dict, period: str = "202604") -> float:
    """Koncerntotal Total Sales (SEK→MSEK) för en period, ur RU-KPI:er."""
    total = sum((c.get("kpis", {}).get(period, {}) or {}).get("sales", 0) or 0
                for c in dash["companies"])
    return total / 1e6


def verify_data_layer(dash: dict) -> None:
    """Grind: koncern Total Sales 202604 ≈ förväntat ankare. Annars SystemExit."""
    got = koncern_sales_msek(dash, "202604")
    exp = EXPECTED_KONCERN_SALES_202604_MSEK
    diff = abs(got - exp) / exp if exp else 0
    log("INFO", f"Koncern Total Sales YTD 202604 = {got:,.0f} MSEK (ankare {exp}, diff {diff:.1%})")
    if diff > KONCERN_SALES_TOLERANCE:
        raise SystemExit(
            f"[ERROR] Datalager-ankaret missar: {got:,.0f} vs {exp} MSEK "
            f"(diff {diff:.1%} > {KONCERN_SALES_TOLERANCE:.0%}). "
            "Stannar innan rendering — kolla queries/FX/best_source.")


def build_dash(period: str) -> tuple[dict, list]:
    """Kör datalagret: queries → aggregate. Returnerar (dash, full_year_only_cids)."""
    con = db_io.connect()
    try:
        raw = db_io.fetch_all(con, period)
    finally:
        con.close()
    fyo = raw["full_year_only_cids"] or []
    log("INFO", f"Hämtade: {len(raw['ytd'])} ytd-rader, {len(raw['companies'])} bolag, "
                f"{len(raw['fx_rates'])} FX-kurser, {len(fyo)} full-year-only-cids")
    dash = build_dashboard_data(raw["ytd"], raw["companies"], raw["personnel"],
                                raw["fx_rates"], fyo)
    log("INFO", f"Byggde {len(dash['companies'])} reporting units")
    return dash, fyo


def main(argv=None) -> None:
    args = parse_args(argv)
    log("START", f"build ytd_nyckeltal  period {args.period}")
    args.output.mkdir(parents=True, exist_ok=True)

    dash, fyo = build_dash(args.period)
    verify_data_layer(dash)

    if args.data_only:
        out = args.output / "dashboard_data.json"
        out.write_text(json.dumps(dash, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        log("DONE", f"data-only: {out}")
        return

    # Validering + AARO-klassificering + rendering.
    validation = None
    aaro_data = None
    do_validate = args.facit_dir and not args.no_validate
    if do_validate:
        import validate  # noqa: E402
        import mercur     # noqa: E402
        import aaro       # noqa: E402
        validation = validate.run(dash, fyo, args.facit_dir, mercur, args.period)
        log("INFO", f"Validering: {len(validation['rows'])} RU-rader mot Mercur")
        aaro_data = aaro.run(args.facit_dir, mercur, args.period)
        log("INFO", f"AARO-klassificering: {len(aaro_data)} konto-rader mot Mercur")

    import render_html  # noqa: E402
    import render_xlsx  # noqa: E402
    html_path = args.output / "Nyckeltal.html"
    xlsx_path = args.output / "Nyckeltal.xlsx"
    render_html.render(dash, validation, _HERE / "templates" / "dashboard_base.html",
                       html_path, aaro_data=aaro_data)
    log("OK", f"HTML: {html_path} ({html_path.stat().st_size // 1024} KB)")
    render_xlsx.render(dash, validation, fyo, xlsx_path, aaro_data=aaro_data)
    log("OK", f"Excel: {xlsx_path}")

    # Persistera mellan-data (post-attach) för felsökning/diff — inte stale.
    (args.output / "dashboard_data.json").write_text(
        json.dumps(dash, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if validation is not None:
        (args.output / "validation.json").write_text(
            json.dumps(validation, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if aaro_data is not None:
        (args.output / "aaro_classification.json").write_text(
            json.dumps(aaro_data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    log("DONE", "build ytd_nyckeltal")


if __name__ == "__main__":
    main()
