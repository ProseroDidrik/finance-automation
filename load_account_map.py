"""Ladda kontoplans-mappning till dim_account_map i DuckDB.

Källa: _params/Dimensionsmedlemmar  Konto.xlsx (export från redovisningssystem).
Filen mappar varje bolags-konto till gruppens konsoliderade kontoplan via en
parent-pointer. Förutsättning för konsoliderad P&L/BS senare.

Layout (header på rad 2, data från rad 3):
  B = ID                       (t.ex. '10_1209', 'Equi', 'B')
  C = Beskrivning              (svensk)
  D = Beskrivning (Engelska)
  E = Aggregerad               '1' = gruppkonto, annars bolagskonto
  I = Källa
  J = Tillhör (Kontoklass)     (parent — annan rads B)

Idempotens: TRUNCATE dim_account_map; INSERT … vid varje körning. Referensdata,
inte period-bunden — senaste laddning vinner totalt. En sammanfattnings-rad
skrivs i load_history med period='REF', source_kind='ACCOUNT_MAP'.

Varför direkt XML-parsning%s
  openpyxl 3.1.5 kraschar på filens stylesheet (Font.fontId-attributet är inte
  godkänt) och duckdb's read_xlsx läser bara kolumn A. Direkt zip+ElementTree
  parsning är robust mot stylesheet-quirks och beror bara på stdlib.
"""
from __future__ import annotations

import argparse
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Iterator

import db
from shared import begin_run, log

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE = REPO_ROOT / "_params" / "Dimensionsmedlemmar  Konto.xlsx"
SOURCE_KIND = "ACCOUNT_MAP"
SENTINEL_PERIOD = "REF"

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
PFX_RE = re.compile(r"^(\d+)_(.*)$")


def _col_letter(cell_ref: str) -> str:
    """'B3' -> 'B', 'AA10' -> 'AA'."""
    out = []
    for ch in cell_ref:
        if ch.isalpha():
            out.append(ch)
        else:
            break
    return "".join(out)


def iter_rows(path: Path) -> Iterator[dict[str, str]]:
    """Yield dict {col_letter: value} per datarad (skipping title + header)."""
    with zipfile.ZipFile(path) as z:
        with z.open("xl/sharedStrings.xml") as f:
            sst_root = ET.parse(f).getroot()
        sst: list[str] = []
        for si in sst_root.findall("s:si", NS):
            t = si.find("s:t", NS)
            sst.append(t.text if t is not None and t.text is not None else "")

        with z.open("xl/worksheets/sheet1.xml") as f:
            ws_root = ET.parse(f).getroot()

    sheet_data = ws_root.find("s:sheetData", NS)
    if sheet_data is None:
        return

    for r in sheet_data.findall("s:row", NS):
        try:
            row_num = int(r.get("r") or "0")
        except ValueError:
            continue
        if row_num < 3:  # rad 1 = titel, rad 2 = header
            continue
        cells: dict[str, str] = {}
        for c in r.findall("s:c", NS):
            ref = c.get("r") or ""
            col = _col_letter(ref)
            v = c.find("s:v", NS)
            if v is None or v.text is None:
                continue
            if c.get("t") == "s":
                try:
                    cells[col] = sst[int(v.text)]
                except (ValueError, IndexError):
                    cells[col] = v.text
            else:
                cells[col] = v.text
        yield cells


def parse_row(cells: dict[str, str]) -> tuple | None:
    """Returnera (account_id, description, description_en, is_aggregated,
    parent_id, source, company_id, account_code) eller None om raden ska droppas.
    """
    account_id = (cells.get("B") or "").strip()
    if not account_id:
        return None  # tom B → drop (50 sådana rader, skulle annars kollidera på PK)

    description = (cells.get("C") or "").strip() or None
    description_en = (cells.get("D") or "").strip() or None
    is_aggregated = (cells.get("E") or "").strip() == "1"
    source = (cells.get("I") or "").strip() or None
    parent_id = (cells.get("J") or "").strip() or None

    company_id: int | None = None
    account_code: str | None = None
    m = PFX_RE.match(account_id)
    if m:
        try:
            company_id = int(m.group(1))
        except ValueError:
            company_id = None
        suffix = (m.group(2) or "").strip()
        account_code = suffix or None

    return (
        account_id, description, description_en, is_aggregated,
        parent_id, source, company_id, account_code,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ladda kontoplans-mappning till dim_account_map."
    )
    parser.add_argument("--source", default=None,
                        help=f"xlsx-fil (default: {DEFAULT_SOURCE.name})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Läs och rapportera, skriv inte till DB")
    args = parser.parse_args()

    src = Path(args.source) if args.source else DEFAULT_SOURCE
    begin_run("load_account_map.py", SENTINEL_PERIOD)
    log("START", "load_account_map.py",
        f"source={src.name} dry_run={args.dry_run}")

    if not src.exists():
        log("ERROR", "scan", f"Källfil saknas: {src}")
        log("DONE", "load_account_map.py", "0 OK  0 WARN  0 SKIP  1 ERROR")
        return

    rows: list[tuple] = []
    skipped = 0
    n_group = 0
    n_bolag = 0
    n_other = 0

    for cells in iter_rows(src):
        parsed = parse_row(cells)
        if parsed is None:
            skipped += 1
            continue
        rows.append(parsed)
        is_agg = parsed[3]
        company_id = parsed[6]
        if is_agg:
            n_group += 1
        elif company_id is not None:
            n_bolag += 1
        else:
            n_other += 1

    log("INFO", "parse",
        f"rader={len(rows)}  gruppkonton={n_group}  bolagskonton={n_bolag}  "
        f"övrigt={n_other}  droppade={skipped}")

    if args.dry_run:
        log("OK", "load_account_map.py",
            f"[DRY] {src.name}  {len(rows)} rader klara att skriva")
        log("DONE", "load_account_map.py", "1 OK  0 WARN  0 SKIP  0 ERROR")
        return

    now = datetime.now()
    rel_src = db.relpath_from_base(src, REPO_ROOT)
    con = db.connect()
    try:
        db.init_schema(con)
        con.execute("BEGIN")
        try:
            con.execute("DELETE FROM dim_account_map")
            con.executemany(
                """INSERT INTO dim_account_map
                   (account_id, description, description_en, is_aggregated,
                    parent_id, source, company_id, account_code, loaded_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [(*r, now) for r in rows],
            )
            con.execute(
                """INSERT INTO load_history
                   (company_id, period, source_kind, source_file, rows_loaded,
                    sum_amount, statement_type_present, status, message, loaded_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [None, SENTINEL_PERIOD, SOURCE_KIND, rel_src, len(rows),
                 None, False, "ok",
                 f"group={n_group} bolag={n_bolag} other={n_other} skipped={skipped}",
                 now],
            )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()

    log("OK", "load_account_map.py",
        f"{src.name}  {len(rows)} rader  ({n_group} grupp + {n_bolag} bolag + "
        f"{n_other} övrigt; {skipped} droppade)")
    log("DONE", "load_account_map.py", "1 OK  0 WARN  0 SKIP  0 ERROR")


if __name__ == "__main__":
    main()
