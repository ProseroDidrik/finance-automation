"""Diagnostikrapport: klassificera SAF-T-journaldubbletter i Klass A vs B.

Syfte: Innan vi designar en städningsstrategi måste vi veta fördelningen
mellan de två klasserna av (bolag, period)-dubbletter:

  Klass A — load_saft.py-bugg b25f397 (fixad 2026-05-21).
            Månads-SAF-T med YTD-overlap, senare fil ÄR mer komplett.
            Säker att auto-städa (MAX(loaded_at)-heuristik fungerar).

  Klass B — SIE_VER-omladdningens samma-batch-fall (2026-05-20).
            Flera FY-exporter laddade samma sekund. Ingen "rätt" fil — alla
            har samma loaded_at, kräver manuell inspektion av SAF-T-filerna.

Klassificeringen baseras på TVÅ signaler per (bolag, period):
  (1) loaded_at-spann mellan första och sista filen
  (2) Filsökvägs-prefix — '_history/' = FY-historik från SIE_VER-omladdningen

Reglerna (konservativa — vid tveksamhet "?", inte "A"):
  Klass A — span > 1 dag OCH INGEN fil i _history/
            → normalt månadsflöde, senare fil är auktoritativ, auto-städbart
  Klass B — ALLA filer i _history/  ELLER  span ≤ 1 dag
            → FY-historik eller samma batch, manuell triage
  "?"     — allt däremellan (fil-mix, ovanlig spann-profil)

Output:
  - Stdout-sammanfattning (antal per klass + per bolag)
  - CSV-rapport till _logs/saft_dup_classes_YYYYMMDD_HHMMSS.csv
    Kolumner: bolag, period, klass, n_files, span_seconds,
              loaded_at_min, loaded_at_max, source_files

INGEN DELETE-logik. Bara läsning. Säkert att köra när som helst.

Användning:
    py scripts/analyze_saft_dup_classes.py
    py scripts/analyze_saft_dup_classes.py --company 9 107 148

Förutsättning: DATABASE_URL satt (admin-anslutning krävs — mcp_readonly:s
60s-timeout räcker inte för större bolag).
    $env:DATABASE_URL = (az keyvault secret show --vault-name kv-finauto-6427 `
                         --name database-url --query value -o tsv)
"""
from __future__ import annotations

import argparse
import csv
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
    pass

import psycopg

import db

STATEMENT_TIMEOUT_MS = 600_000  # 10 min per query (stora bolag tar tid)
WORK_MEM = "128MB"

# Tröskel: span > 1 dag krävs för Klass A (verklig månadsflödes-separation).
SPAN_A_MIN_SEC = 86_400


def classify(span_sec: float, source_files: list[str]) -> str:
    """Klassificera ett (bolag, period)-dubblettpar.

    Konservativ: vid tveksamhet returneras "?" (manuell inspektion krävs),
    inte "A". Bara tydliga normalt-månadsflöde-fall blir A.
    """
    all_history = all("_history/" in sf for sf in source_files)
    any_history = any("_history/" in sf for sf in source_files)

    if all_history:
        # Alla filer är FY-export från SIE_VER-omladdningen.
        # Per memory: ingen tie-break möjlig, alla har ~samma loaded_at.
        return "B"
    if span_sec >= SPAN_A_MIN_SEC and not any_history:
        # Tydlig månadsflödes-separation utan blandning med FY-historik.
        return "A"
    # Allt annat: mixad, kort spann, eller delvis FY-historik. Triage.
    return "?"


def list_saft_companies(con, filter_ids: list[int] | None) -> list[tuple[int, str, str]]:
    rows = con.execute(
        """SELECT company_id, name, country FROM dim_company
           WHERE country IN ('Norway','Denmark')
             AND COALESCE(kind,'') != 'consolidated'
           ORDER BY company_id"""
    ).fetchall()
    if filter_ids:
        rows = [r for r in rows if r[0] in filter_ids]
    return rows


def analyze_company(con, company_id: int) -> list[dict]:
    """Returnerar lista av dubblettpar för bolaget. Tomt om inga dubbletter."""
    rows = con.execute(
        """SELECT period, source_file, COUNT(*) AS n_rows,
                  MIN(loaded_at) AS loaded_min,
                  MAX(loaded_at) AS loaded_max
           FROM fact_journal_saft
           WHERE company_id = %s
           GROUP BY period, source_file
           ORDER BY period, MAX(loaded_at)""",
        [company_id],
    ).fetchall()

    by_period: dict[str, list] = defaultdict(list)
    for period, sf, n_rows, lmin, lmax in rows:
        # En enskild (bolag, period, source_file) har samma loaded_at för alla
        # rader (de laddades i en batch) — lmin == lmax. Vi använder lmax som
        # filens "när laddades". MIN/MAX-spannet beräknas över olika filer.
        by_period[period].append({
            "source_file": sf,
            "n_rows": n_rows,
            "loaded_at": lmax,
        })

    pairs: list[dict] = []
    for period, entries in by_period.items():
        if len(entries) < 2:
            continue
        loaded_times = [e["loaded_at"] for e in entries]
        lmin, lmax = min(loaded_times), max(loaded_times)
        span_sec = (lmax - lmin).total_seconds()
        entries_sorted = sorted(entries, key=lambda e: e["loaded_at"])
        source_files = [e["source_file"] for e in entries_sorted]
        pairs.append({
            "company_id": company_id,
            "period": period,
            "klass": classify(span_sec, source_files),
            "n_files": len(entries),
            "span_seconds": span_sec,
            "loaded_at_min": lmin,
            "loaded_at_max": lmax,
            "source_files": ";".join(e["source_file"] for e in entries_sorted),
            "total_rows": sum(e["n_rows"] for e in entries),
        })
    return pairs


def run(filter_ids: list[int] | None) -> int:
    t0 = time.time()
    print(f"[{datetime.now():%H:%M:%S}] SAF-T dup-class analyzer start", flush=True)

    con = db.connect(read_only=True, role="legacy")
    try:
        with con.raw.cursor() as cur:
            cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
            cur.execute(f"SET work_mem = '{WORK_MEM}'")

        companies = list_saft_companies(con, filter_ids)
        print(f"  Analyserar {len(companies)} bolag (NO+DK)", flush=True)

        all_pairs: list[dict] = []
        failed: list[tuple[int, str, str]] = []

        for cid, name, country in companies:
            t1 = time.time()
            try:
                pairs = analyze_company(con, cid)
            except psycopg.errors.QueryCanceled:
                elapsed = time.time() - t1
                print(f"    [{cid:>4}] {country:7} {name[:25]:25}  TIMEOUT >{elapsed:.0f}s",
                      flush=True)
                failed.append((cid, name, "timeout"))
                continue
            except Exception as e:
                print(f"    [{cid:>4}] {country:7} {name[:25]:25}  ERROR {type(e).__name__}: {e}",
                      flush=True)
                failed.append((cid, name, f"error: {type(e).__name__}"))
                continue
            elapsed = time.time() - t1

            if not pairs:
                print(f"    [{cid:>4}] {country:7} {name[:25]:25}  0 dup  ({elapsed:.1f}s)",
                      flush=True)
                continue

            class_counts = defaultdict(int)
            for p in pairs:
                class_counts[p["klass"]] += 1
            summary = " ".join(f"{k}={v}" for k, v in sorted(class_counts.items()))
            print(f"    [{cid:>4}] {country:7} {name[:25]:25}  "
                  f"{len(pairs):>3} dup  ({summary})  ({elapsed:.1f}s)",
                  flush=True)
            all_pairs.extend(pairs)

        # Sammanfattning
        print(flush=True)
        print(f"=== Sammanfattning  ({time.time()-t0:.1f}s total) ===", flush=True)
        print(f"Bolag analyserade: {len(companies)}", flush=True)
        print(f"Bolag med dubbletter: {len(set(p['company_id'] for p in all_pairs))}", flush=True)
        print(f"Totala dubblettpar: {len(all_pairs)}", flush=True)
        print(flush=True)

        total_class = defaultdict(int)
        total_rows = defaultdict(int)
        for p in all_pairs:
            total_class[p["klass"]] += 1
            total_rows[p["klass"]] += p["total_rows"]
        print(f"{'Klass':<6}  {'Par':>5}  {'Rader':>10}  Tolkning", flush=True)
        labels = {
            "A": "Säkert auto-städbar (>1d spann, inga _history/-filer)",
            "B": "Manuell triage (alla filer i _history/, ingen tie-break)",
            "?": "Gråzon (mixad fil-profil eller kort spann) — bedöm fall för fall",
        }
        for klass in ["A", "B", "?"]:
            n = total_class.get(klass, 0)
            r = total_rows.get(klass, 0)
            print(f"{klass:<6}  {n:>5}  {r:>10,}  {labels[klass]}", flush=True)

        # Per-bolag-fördelning för B (intressantast för triage)
        if total_class.get("B", 0) > 0 or total_class.get("?", 0) > 0:
            print(flush=True)
            print("=== Bolag med Klass B eller ? (triagebehov) ===", flush=True)
            by_company: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
            for p in all_pairs:
                if p["klass"] in ("B", "?"):
                    by_company[p["company_id"]][p["klass"]] += 1
            for cid in sorted(by_company.keys()):
                counts = by_company[cid]
                tag = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
                print(f"  bolag {cid:>4}: {tag}", flush=True)

        if failed:
            print(flush=True)
            print(f"=== FAILED ({len(failed)} bolag) ===", flush=True)
            for cid, name, reason in failed:
                print(f"  {cid} {name}: {reason}", flush=True)

        # CSV-rapport
        log_dir = Path(__file__).resolve().parent.parent / "_logs"
        log_dir.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = log_dir / f"saft_dup_classes_{stamp}.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["company_id", "period", "klass", "n_files",
                             "span_seconds", "loaded_at_min", "loaded_at_max",
                             "total_rows", "source_files"])
            for p in sorted(all_pairs, key=lambda x: (x["klass"], x["company_id"], x["period"])):
                writer.writerow([
                    p["company_id"], p["period"], p["klass"], p["n_files"],
                    f"{p['span_seconds']:.1f}",
                    p["loaded_at_min"].isoformat(),
                    p["loaded_at_max"].isoformat(),
                    p["total_rows"], p["source_files"],
                ])
        print(flush=True)
        print(f"CSV-rapport: {csv_path}", flush=True)
        print(f"[{datetime.now():%H:%M:%S}] done", flush=True)
        return 1 if failed else 0
    finally:
        con.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--company", type=int, nargs="+",
                   help="Begränsa till dessa bolag (default: alla NO+DK).")
    args = p.parse_args()
    return run(args.company)


if __name__ == "__main__":
    sys.exit(main())
