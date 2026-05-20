"""Radera utfalls-data från fact_balances per (bolag, period, källa).

Källan (--source_kind) krävs alltid — det finns ingen "radera allt"-knapp.
Lager-isolering: anges källa = IMP rörs bara IMP-lagret (per land), aldrig
MAN/IMP_ADJ. Anges källa = MAN rörs bara MAN, etc.

IMP-mappning per land (filformat-info bevaras i source_kind):
  Sweden, CA          → DELETE source_kind IN ('SIE','SIE_PSALDO') hela FY:t
                        + fact_journal_sie för samma FY
  Norway              → DELETE source_kind = 'SAFT' hela FY:t
                        + fact_journal_saft för samma FY
  Finland/Denmark/
  Germany/CENTR       → DELETE source_kind = 'IMP' för perioden

IMP_ADJ / MAN: alltid per (bolag, period, källa) — oavsett land.

Räkenskapsår-härledning (för SE/NO IMP-radering): från löneårets dim_period
om brutet FY skulle förekomma — annars kalenderår. (Dagens flöde antar
kalenderår; om brutna FY införs senare bör härledningen läsa från senaste
laddade SIE/SAFT-fil.)

Körning:
  py delete_db.py --period 202604 --source_kind IMP --dry-run
  py delete_db.py --period 202604 --source_kind IMP --company 134 196
  py delete_db.py --period 202604 --source_kind IMP --country Sweden
  py delete_db.py --period 202604 --source_kind MAN
"""
from __future__ import annotations

import argparse
from datetime import datetime

import db
from shared import begin_run, log

VALID_KINDS = ("IMP", "IMP_ADJ", "MAN")
VALID_COUNTRIES = ("Sweden", "Norway", "Finland", "Denmark", "Germany", "CENTR", "CA")


def _fy_range_for_period(period: str) -> tuple[str, str]:
    """Räkenskapsår enligt kalenderår — fy_start, fy_end som 'YYYYMM'."""
    year = period[:4]
    return f"{year}01", f"{year}12"


def _resolve_companies(con: db.Conn,
                       company_ids: list[int] | None,
                       country: str | None) -> list[tuple[int, str, str]]:
    """Returnerar (company_id, name, country) för bolag som matchar filter."""
    sql = "SELECT company_id, name, country FROM dim_company"
    where: list[str] = []
    params: list = []
    if company_ids:
        placeholders = ",".join(["%s"] * len(company_ids))
        where.append(f"company_id IN ({placeholders})")
        params.extend(company_ids)
    if country:
        where.append("country = %s")
        params.append(country)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY country, company_id"
    return con.execute(sql, params).fetchall()


def _delete_targets_for_company(country: str, source_kind: str, period: str
                                ) -> list[tuple[str, list[str], str]]:
    """Returnerar lista av (table, source_kinds, period_clause) som ska raderas.

    period_clause är en av: 'eq' (= period) eller 'fy' (BETWEEN fy_start AND fy_end).
    source_kinds är de fact_balances-source_kinds som ska träffas (tom lista =
    journal-tabell utan source_kind-filter).
    """
    if source_kind == "IMP":
        if country in ("Sweden", "CA"):
            return [
                ("fact_balances",     ["SIE", "SIE_PSALDO", "SIE_VER"], "fy"),
                ("fact_journal_sie",  [],                    "fy"),
            ]
        if country == "Norway":
            return [
                ("fact_balances",     ["SAFT"], "fy"),
                ("fact_journal_saft", [],       "fy"),
            ]
        # Finland, Denmark, Germany, CENTR — Excel-import per månad
        return [("fact_balances", ["IMP"], "eq")]
    # IMP_ADJ och MAN är per-månad oavsett land
    return [("fact_balances", [source_kind], "eq")]


def _count_rows(con: db.Conn, table: str,
                source_kinds: list[str], company_id: int,
                period: str, fy_start: str, fy_end: str,
                period_clause: str) -> int:
    where = ["company_id = %s"]
    params: list = [company_id]
    if source_kinds:
        placeholders = ",".join(["%s"] * len(source_kinds))
        where.append(f"source_kind IN ({placeholders})")
        params.extend(source_kinds)
    if period_clause == "eq":
        where.append("period = %s")
        params.append(period)
    else:  # fy
        where.append("period BETWEEN %s AND %s")
        params.extend([fy_start, fy_end])
    sql = f"SELECT COUNT(*) FROM {table} WHERE " + " AND ".join(where)
    return con.execute(sql, params).fetchone()[0]


def _delete_rows(con: db.Conn, table: str,
                 source_kinds: list[str], company_id: int,
                 period: str, fy_start: str, fy_end: str,
                 period_clause: str) -> None:
    where = ["company_id = %s"]
    params: list = [company_id]
    if source_kinds:
        placeholders = ",".join(["%s"] * len(source_kinds))
        where.append(f"source_kind IN ({placeholders})")
        params.extend(source_kinds)
    if period_clause == "eq":
        where.append("period = %s")
        params.append(period)
    else:
        where.append("period BETWEEN %s AND %s")
        params.extend([fy_start, fy_end])
    sql = f"DELETE FROM {table} WHERE " + " AND ".join(where)
    con.execute(sql, params)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Radera utfalls-data från fact_balances (+journal-tabeller "
                    "för SE/NO IMP). --source_kind krävs alltid."
    )
    parser.add_argument("--period", required=True,
                        help="YYYYMM. För IMP på SE/NO raderas hela FY:t som perioden tillhör.")
    parser.add_argument("--source_kind", required=True, choices=VALID_KINDS,
                        help="Vilket lager: IMP (auto-import), IMP_ADJ (justering), MAN (manuell).")
    parser.add_argument("--company", type=int, nargs="*", metavar="ID",
                        help="Begränsa till dessa bolag (default: alla).")
    parser.add_argument("--country", choices=VALID_COUNTRIES, default=None,
                        help="Begränsa till bolag i detta land.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Lista vad som skulle raderas utan att köra DELETE.")
    args = parser.parse_args()

    if len(args.period) != 6 or not args.period.isdigit():
        parser.error(f"--period måste vara YYYYMM, fick {args.period!r}")

    begin_run("delete_db.py", args.period)
    log("START", "delete_db.py",
        f"period={args.period} source_kind={args.source_kind} "
        f"company={args.company or 'alla'} country={args.country or 'alla'} "
        f"dry_run={args.dry_run}")

    fy_start, fy_end = _fy_range_for_period(args.period)

    con = db.connect()
    try:
        db.init_schema(con)
        companies = _resolve_companies(con, args.company, args.country)
        if not companies:
            log("WARN", "scan", "Inga bolag matchar filter.")
            log("DONE", "delete_db.py", "0 OK  0 SKIP  0 ERROR")
            return

        ok = 0
        skip = 0
        error = 0
        total_rows = 0

        for company_id, name, country in companies:
            try:
                targets = _delete_targets_for_company(country, args.source_kind, args.period)
                rows_per_target: list[tuple[str, list[str], str, int]] = []
                co_total = 0
                for table, source_kinds, clause in targets:
                    n = _count_rows(con, table, source_kinds, company_id,
                                    args.period, fy_start, fy_end, clause)
                    rows_per_target.append((table, source_kinds, clause, n))
                    co_total += n

                if co_total == 0:
                    log("SKIP", company_id,
                        f"{name} ({country}): 0 rader att radera")
                    skip += 1
                    continue

                detail = ", ".join(
                    f"{t}({'+'.join(sk) or 'alla'},{'FY' if c=='fy' else args.period})={n}"
                    for t, sk, c, n in rows_per_target if n > 0
                )

                if args.dry_run:
                    log("INFO", company_id,
                        f"[DRY] {name} ({country}): {co_total} rader  [{detail}]")
                    ok += 1
                    total_rows += co_total
                    continue

                con.execute("BEGIN")
                try:
                    for table, source_kinds, clause, _ in rows_per_target:
                        _delete_rows(con, table, source_kinds, company_id,
                                     args.period, fy_start, fy_end, clause)
                    con.execute(
                        """INSERT INTO load_history
                           (company_id, period, source_kind, source_file, rows_loaded,
                            sum_amount, statement_type_present, status, message, loaded_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        [company_id, args.period, args.source_kind, "(delete_db.py)",
                         -co_total, 0.0, False, "ok",
                         f"DELETE {detail}", datetime.now()],
                    )
                    con.execute("COMMIT")
                except Exception as e:
                    con.execute("ROLLBACK")
                    log("ERROR", company_id, f"DB-fel: {e}")
                    error += 1
                    continue

                log("OK", company_id,
                    f"{name} ({country}): raderade {co_total} rader  [{detail}]")
                ok += 1
                total_rows += co_total
            except Exception as e:
                log("ERROR", company_id, f"Fel: {e}")
                error += 1

        verb = "skulle raderas" if args.dry_run else "raderade"
        log("DONE", "delete_db.py",
            f"{ok} OK  {skip} SKIP  {error} ERROR  ({total_rows} rader {verb})")
    finally:
        con.close()


if __name__ == "__main__":
    main()
