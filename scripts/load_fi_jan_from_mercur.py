#!/usr/bin/env python3
"""Ersätt {land} {period} IMP i fact_balances med data från backup_from_mercur.

Bakgrund: Excel-exporter (Fennoa-CSV, Susa-pro-monat m.fl.) rapporterar
"ändring under perioden" där startdatum = räkenskapsårets början (1 jan).
För januari betyder det att kol 1 visar saldot per 31 jan (= IB + rörelse)
istället för bara rörelsen. Mercur räknar däremot jan-rörelse = saldo 31 jan
minus saldo 31 dec föregående år (utan IB). Vi har inte saldot per 31 dec
föregående år i fact_balances för att korrigera Excel-input — backup_from_mercur
har korrekta rörelser direkt, använd den.

Standard: --country Finland --period 202601. Stöder också Denmark, Germany.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--country", default="Finland",
                        choices=["Finland", "Denmark", "Germany"],
                        help="Land som ska ersättas. Default: Finland")
    parser.add_argument("--period", default="202601",
                        help="Period att ersätta (YYYYMM). Default: 202601")
    parser.add_argument("--dry-run", "-n", action="store_true")
    args = parser.parse_args()

    country = args.country
    period = args.period

    con = db.connect()
    try:
        before = con.execute(
            """SELECT COUNT(*), COALESCE(SUM(amount), 0)
               FROM fact_balances fb JOIN dim_company c USING (company_id)
               WHERE c.country=%s AND fb.period=%s
                 AND fb.source_kind='IMP' AND fb.scenario='A'""",
            [country, period],
        ).fetchone()
        backup_per_bolag = con.execute(
            """SELECT b.company_id, c.name, COUNT(*), COALESCE(SUM(b.amount), 0)
               FROM backup_from_mercur b JOIN dim_company c USING (company_id)
               WHERE c.country=%s AND b.period=%s
                 AND b.source_kind='IMP' AND b.scenario='A'
               GROUP BY b.company_id, c.name ORDER BY b.company_id""",
            [country, period],
        ).fetchall()
        total_backup_rows = sum(r[2] for r in backup_per_bolag)
        total_backup_sum = sum(r[3] for r in backup_per_bolag)

        print(f"FACT idag ({country} {period} IMP A): {before[0]} rader, sum={before[1]:.2f}")
        print(f"BACKUP-källa: {len(backup_per_bolag)} bolag, "
              f"{total_backup_rows} rader, sum={total_backup_sum:.2f}")
        print()
        for company_id, name, nrows, nsum in backup_per_bolag:
            print(f"  {company_id:>3} {name:<35} {nrows:>4} rader  sum={nsum:>14.2f}")

        if args.dry_run:
            print("\n[DRY-RUN] inga ändringar.")
            return

        now = datetime.now()
        source_label = f"backup_from_mercur ({country} {period} source override)"

        con.execute("BEGIN")
        try:
            con.execute(
                """DELETE FROM fact_balances
                   WHERE company_id IN (SELECT company_id FROM dim_company WHERE country=%s)
                     AND period=%s AND source_kind='IMP' AND scenario='A'""",
                [country, period],
            )
            con.execute(
                """INSERT INTO fact_balances (company_id, period, period_type, account_code,
                       account_name, amount, currency, statement_type, source_kind,
                       source_file, row_index, scenario, loaded_at)
                   SELECT b.company_id, b.period, 'monthly', b.account_code,
                          b.account_name, b.amount, b.currency, NULL, b.source_kind,
                          %s, b.row_index, b.scenario, %s
                   FROM backup_from_mercur b JOIN dim_company c USING (company_id)
                   WHERE c.country=%s AND b.period=%s
                     AND b.source_kind='IMP' AND b.scenario='A'""",
                [source_label, now, country, period],
            )
            for company_id, _name, nrows, nsum in backup_per_bolag:
                con.execute(
                    """INSERT INTO load_history (company_id, period, source_kind, source_file,
                           rows_loaded, sum_amount, status, message, loaded_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    [company_id, period, "IMP", source_label,
                     nrows, float(nsum), "ok",
                     f"Källa: backup_from_mercur pga fiscal-year-reset-quirk i Excel-export",
                     now],
                )
            con.execute("COMMIT")
            print(f"\nOK. Kopierade {total_backup_rows} rader för {len(backup_per_bolag)} {country}-bolag.")
        except Exception as e:
            con.execute("ROLLBACK")
            print(f"\nFEL: {e}")
            sys.exit(1)
    finally:
        con.close()


if __name__ == "__main__":
    main()
