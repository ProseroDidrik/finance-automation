"""Städa historiska dubbletter i fact_journal_saft (per-bolag, natt-säker).

Bakgrund: före load_saft.py-fixen b25f397 (2026-05-21) deduppade SAF-T-loadern
per source_file istället för per (company_id, period). Norska bolag vars
månads-SAF-T innehåller flera månaders GL fick varje månad sparad en gång per
efterföljande YTD-fil → 2-4x dubbelräkning. Kodfixen stoppar NYA dubbletter;
detta skript städar historiken.

Strategi: speglar exakt vad load_saft.py NU skulle gjort vid omladdning ---
för varje (company_id, period) med flera source_files, behåll bara raderna
från den source_file som har högst loaded_at (senast laddade källan).

Varför per-bolag: full-tabell-scan på fact_journal_saft (4.5M rader) tar
>10 min på Burstable B1ms. Per-bolag via idx_fjsaft_company_period är O(B*S)
där B = SAFT-bolag (~43) och S = bolagsstorlek. Större bolag tar 1-3 min/st;
totalt ~30-60 min — körbart över natten.

Varje bolag städas i egen transaktion. Failar ett bolag fortsätter skriptet
med nästa. Progress loggas till stdout + valfri --log-fil.

Säkerhet:
 - Rapport-siffror påverkas INTE (best_source läser fact_balances, ej journal).
 - --dry-run räknar utan att skriva.
 - --execute kör DELETE per bolag transaktionellt.
 - scripts/check_saft_journal_dups.py är regressionstest — kör efter städning.

Användning:
    py scripts/cleanup_saft_journal_dups.py --dry-run
    py scripts/cleanup_saft_journal_dups.py --execute
    py scripts/cleanup_saft_journal_dups.py --execute --log _logs/saft_cleanup.log
    py scripts/cleanup_saft_journal_dups.py --execute --company 107 148  # bara dessa

Förutsättning: DATABASE_URL satt (pgadmin krävs för DELETE).
    $env:DATABASE_URL = (az keyvault secret show --vault-name kv-finauto-6427 `
                         --name database-url --query value -o tsv)

Natt-körning via Windows Task Scheduler — se scripts/run_saft_cleanup_night.ps1
för wrapper + scheduling-kommando.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows cp1252-konsol kraschar på ø/å i norska bolagsnamn — tvinga utf-8.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass  # äldre Python utan reconfigure

import psycopg

import db

# 10 min per bolag — stora norska bolag (107, 148, 158, 189) kan ta flera min
# på Burstable IO även via index. Räcker väl för den största.
STATEMENT_TIMEOUT_MS = 600_000
WORK_MEM = "128MB"


def _log_writer(log_path: Path | None):
    """Returnerar (write, close) — write skickar till stdout + valfri fil."""
    fh = log_path.open("a", encoding="utf-8") if log_path else None

    def write(line: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        msg = f"[{ts}] {line}"
        print(msg, flush=True)
        if fh:
            fh.write(msg + "\n")
            fh.flush()

    def close() -> None:
        if fh:
            fh.close()

    return write, close


def list_saft_companies(con) -> list[tuple[int, str, str]]:
    """Norge + Danmark i dim_company (potentiella SAFT-bolag)."""
    rows = con.execute(
        """SELECT company_id, name, country FROM dim_company
           WHERE country IN ('Norway','Denmark')
             AND COALESCE(kind,'') != 'consolidated'
           ORDER BY company_id"""
    ).fetchall()
    return rows


def analyze_company(con, company_id: int) -> dict:
    """Hämta (period, source_file)-triplar för ett bolag. Returnerar dict med
    'pairs' (dubblettpar), 'losers' (rader att radera), 'winner_files'.

    Använder idx_fjsaft_company_period — sekundärt index gör detta snabbt
    även när tabellen är 4.5M rader totalt.
    """
    rows = con.execute(
        """SELECT period, source_file, COUNT(*) AS n_rows, MAX(loaded_at) AS last_loaded
           FROM fact_journal_saft
           WHERE company_id = %s
           GROUP BY period, source_file""",
        [company_id],
    ).fetchall()

    by_period: dict[str, list] = defaultdict(list)
    for period, sf, n_rows, loaded in rows:
        by_period[period].append((sf, n_rows, loaded))

    dup_pairs = {p: v for p, v in by_period.items() if len(v) > 1}
    losers: list[tuple[str, str, int]] = []  # (period, source_file, n_rows)
    for period, entries in dup_pairs.items():
        # Behåll först (= högst loaded_at)
        entries_sorted = sorted(entries, key=lambda e: e[2], reverse=True)
        for sf, n_rows, _loaded in entries_sorted[1:]:
            losers.append((period, sf, n_rows))

    return {
        "n_triples": len(rows),
        "n_periods": len(by_period),
        "n_dup_pairs": len(dup_pairs),
        "losers": losers,
        "loser_row_count": sum(n for _, _, n in losers),
    }


def execute_cleanup(con, company_id: int, losers: list[tuple[str, str, int]]) -> int:
    """DELETE losers för ett bolag i en transaktion. Returnerar antal raderade.

    Bygger (period, source_file)-par och kör en enda DELETE med VALUES-join.
    """
    if not losers:
        return 0
    # psycopg accepterar list of tuples för composite IN via mogrify-mönstret —
    # men cleanast är en VALUES-tabell.
    placeholders = ",".join(["(%s,%s)"] * len(losers))
    params: list = []
    for period, sf, _ in losers:
        params.extend([period, sf])

    con.execute("BEGIN")
    try:
        with con.raw.cursor() as cur:
            cur.execute(
                f"""DELETE FROM fact_journal_saft
                    WHERE company_id = %s
                      AND (period, source_file) IN ({placeholders})""",
                [company_id, *params],
            )
            deleted = cur.rowcount
        con.execute("COMMIT")
        return deleted
    except Exception:
        con.execute("ROLLBACK")
        raise


def run(execute: bool, log_path: Path | None, companies_filter: list[int] | None) -> int:
    write, close = _log_writer(log_path)
    t0 = time.time()
    write(f"SAF-T cleanup start  mode={'EXECUTE' if execute else 'DRY-RUN'}")
    if log_path:
        write(f"Log: {log_path}")

    con = db.connect(read_only=not execute, role="legacy")
    try:
        # Session-tuning: större work_mem + lång timeout per query.
        with con.raw.cursor() as cur:
            cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
            cur.execute(f"SET work_mem = '{WORK_MEM}'")

        companies = list_saft_companies(con)
        if companies_filter:
            companies = [c for c in companies if c[0] in companies_filter]
        write(f"Bolag att analysera: {len(companies)}")

        total_deleted = 0
        total_dup_pairs = 0
        failed_companies: list[tuple[int, str]] = []

        for cid, name, country in companies:
            t1 = time.time()
            try:
                info = analyze_company(con, cid)
            except psycopg.errors.QueryCanceled:
                elapsed = time.time() - t1
                write(f"  [{cid:>4}] {country:7} {name[:25]:25}  TIMEOUT (>{elapsed:.0f}s)")
                failed_companies.append((cid, "timeout-analyze"))
                continue
            except Exception as e:
                elapsed = time.time() - t1
                write(f"  [{cid:>4}] {country:7} {name[:25]:25}  ERROR {type(e).__name__}: {e}")
                failed_companies.append((cid, f"error-analyze: {type(e).__name__}"))
                continue

            elapsed = time.time() - t1
            if info["n_dup_pairs"] == 0:
                write(f"  [{cid:>4}] {country:7} {name[:25]:25}  "
                      f"{info['n_triples']:>4} tr  0 dup  ({elapsed:.1f}s)")
                continue

            write(f"  [{cid:>4}] {country:7} {name[:25]:25}  "
                  f"{info['n_triples']:>4} tr  {info['n_dup_pairs']:>3} dup pairs  "
                  f"{info['loser_row_count']:>6} loser rows  ({elapsed:.1f}s)")
            total_dup_pairs += info["n_dup_pairs"]

            if execute:
                t2 = time.time()
                try:
                    deleted = execute_cleanup(con, cid, info["losers"])
                    total_deleted += deleted
                    write(f"           DELETE: {deleted:>6} rader  ({time.time()-t2:.1f}s)")
                except Exception as e:
                    write(f"           DELETE FAIL: {type(e).__name__}: {e}")
                    failed_companies.append((cid, f"error-delete: {type(e).__name__}"))

        write("")
        write(f"Sammanfattning  ({time.time()-t0:.1f}s total)")
        write(f"  Analyserade: {len(companies)} bolag")
        write(f"  Dubblett-par: {total_dup_pairs}")
        write(f"  Rader raderade: {total_deleted}" if execute else f"  (dry-run, inga ändringar)")
        if failed_companies:
            write(f"  FAILED: {len(failed_companies)} bolag")
            for cid, reason in failed_companies:
                write(f"    {cid}: {reason}")
        write("Klart.")
        return 1 if failed_companies else 0
    finally:
        con.close()
        close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true",
                   help="Analysera utan att skriva.")
    g.add_argument("--execute", action="store_true",
                   help="Kör DELETE per bolag i egna transaktioner.")
    p.add_argument("--log", type=Path,
                   help="Skriv progress även till denna fil (append).")
    p.add_argument("--company", type=int, nargs="+",
                   help="Begränsa till dessa bolag (default: alla NO+DK).")
    args = p.parse_args()

    return run(args.execute, args.log, args.company)


if __name__ == "__main__":
    sys.exit(main())
