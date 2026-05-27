"""Ad-hoc konto-nivå-jämförelse: backup_from_mercur (cumsum YTD) vs fact_balances (YTD).

KORREKT SEMANTIK (verifierat 2026-05-27):
  backup_from_mercur = MÅNADSRÖRELSE per (bolag, period, konto)
                     → cumsum jan→period = YTD
  fact_balances:
    SIE / SAFT  = YTD direkt (kan saknas för tidiga perioder om laddats med --override)
    SIE_PSALDO  = månadsrörelse → cumsum YTD
    IMP         = månadsrörelse → cumsum YTD

Jämförelse: YTD vs YTD per (bolag, period, konto).

OBS: backup_from_mercur har dim-uppdelade rader → SUM per konto.
OBS: SE-tecken: SIE-konv (intäkt -) flippas för match mot Mercur (intäkt +).
"""
from __future__ import annotations

import os

import psycopg


def main() -> None:
    url = os.environ["DATABASE_URL_ETL"]
    with psycopg.connect(url) as con:
        # Mercur YTD = cumsum av månadsvärden per konto
        con.execute("""
            CREATE TEMP VIEW m_ytd AS
            WITH monthly AS (
                SELECT company_id, period, account_code, SUM(amount) AS amount
                FROM backup_from_mercur
                WHERE scenario = 'A'
                  AND period BETWEEN '202601' AND '202604'
                  AND LEFT(account_code, 1) IN ('3','4','5','6','7','8','9')
                GROUP BY company_id, period, account_code
            )
            SELECT a.company_id, a.period, a.account_code,
                   SUM(b.amount) AS amount
            FROM monthly a
            JOIN monthly b
              ON b.company_id = a.company_id
             AND b.account_code = a.account_code
             AND b.period <= a.period
            GROUP BY a.company_id, a.period, a.account_code
        """)

        # DB YTD — välj bästa källa
        con.execute("""
            CREATE TEMP VIEW db_ytd AS
            WITH sie AS (
                SELECT company_id, period, account_code,
                       -amount AS amount, 'SIE' AS source, 1 AS prio
                FROM fact_balances
                WHERE source_kind = 'SIE' AND scenario = 'A'
                  AND period BETWEEN '202601' AND '202604'
                  AND LEFT(account_code, 1) IN ('3','4','5','6','7','8','9')
            ),
            saft AS (
                SELECT company_id, period, account_code,
                       -amount AS amount, 'SAFT' AS source, 2 AS prio
                FROM fact_balances
                WHERE source_kind = 'SAFT' AND scenario = 'A'
                  AND period BETWEEN '202601' AND '202604'
                  AND LEFT(account_code, 1) IN ('3','4','5','6','7','8','9')
            ),
            -- PSALDO cumsum (om SIE saknas — t.ex. för bolag som bara laddat PSALDO)
            psaldo_m AS (
                SELECT company_id, period, account_code,
                       -SUM(amount) AS amount
                FROM fact_balances
                WHERE source_kind = 'SIE_PSALDO' AND scenario = 'A'
                  AND period BETWEEN '202601' AND '202604'
                  AND LEFT(account_code, 1) IN ('3','4','5','6','7','8','9')
                GROUP BY company_id, period, account_code
            ),
            psaldo_ytd AS (
                SELECT a.company_id, a.period, a.account_code,
                       SUM(b.amount) AS amount,
                       'PSALDO_CUMSUM' AS source, 3 AS prio
                FROM psaldo_m a
                JOIN psaldo_m b ON b.company_id=a.company_id AND b.account_code=a.account_code
                                 AND b.period <= a.period
                GROUP BY a.company_id, a.period, a.account_code
            ),
            -- IMP cumsum (FI/DK/DE INL, monthly)
            imp_m AS (
                SELECT company_id, period, account_code, amount
                FROM fact_balances
                WHERE source_kind = 'IMP' AND scenario = 'A'
                  AND period BETWEEN '202601' AND '202604'
                  AND LEFT(account_code, 1) IN ('3','4','5','6','7','8','9')
            ),
            imp_ytd AS (
                SELECT a.company_id, a.period, a.account_code,
                       SUM(b.amount) AS amount,
                       'IMP_CUMSUM' AS source, 4 AS prio
                FROM imp_m a
                JOIN imp_m b ON b.company_id=a.company_id AND b.account_code=a.account_code
                              AND b.period <= a.period
                GROUP BY a.company_id, a.period, a.account_code
            ),
            all_src AS (
                SELECT * FROM sie
                UNION ALL SELECT * FROM saft
                UNION ALL SELECT * FROM psaldo_ytd
                UNION ALL SELECT * FROM imp_ytd
            )
            SELECT DISTINCT ON (company_id, period, account_code)
                   company_id, period, account_code, amount, source
            FROM all_src
            ORDER BY company_id, period, account_code, prio
        """)

        print("=== YTD-jämförelse: backup_from_mercur (cumsum) vs fact_balances (YTD) ===")
        print("(R-konton 3XXX-9XXX, jan-apr 2026, |diff| < 1 kr = ok)")
        print()

        cur = con.execute("""
            WITH joined AS (
                SELECT
                    COALESCE(m.company_id, d.company_id) AS company_id,
                    COALESCE(m.period, d.period) AS period,
                    COALESCE(m.account_code, d.account_code) AS account_code,
                    COALESCE(m.amount, 0) AS m_amt,
                    COALESCE(d.amount, 0) AS d_amt,
                    COALESCE(m.amount, 0) - COALESCE(d.amount, 0) AS diff
                FROM m_ytd m
                FULL OUTER JOIN db_ytd d
                  ON m.company_id = d.company_id
                 AND m.period = d.period
                 AND m.account_code = d.account_code
            )
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE ABS(diff) < 1.0) AS ok,
                COUNT(*) FILTER (WHERE ABS(diff) >= 1.0 AND ABS(diff) < 100) AS lt100,
                COUNT(*) FILTER (WHERE ABS(diff) >= 100 AND ABS(diff) < 1000) AS lt1k,
                COUNT(*) FILTER (WHERE ABS(diff) >= 1000 AND ABS(diff) < 10000) AS lt10k,
                COUNT(*) FILTER (WHERE ABS(diff) >= 10000) AS ge10k,
                COUNT(*) FILTER (WHERE m_amt = 0 AND d_amt != 0) AS only_in_db,
                COUNT(*) FILTER (WHERE d_amt = 0 AND m_amt != 0) AS only_in_mercur
            FROM joined
        """)
        r = cur.fetchone()
        total, ok, lt100, lt1k, lt10k, ge10k, only_db, only_m = r
        print(f"Totalt (bolag×period×konto): {total:,}")
        print(f"  ok (|diff| < 1 kr)         : {ok:>7,}  ({100*ok/total:>5.1f}%)")
        print(f"  diff 1–100 kr              : {lt100:>7,}  ({100*lt100/total:>5.1f}%)")
        print(f"  diff 100–1000 kr           : {lt1k:>7,}  ({100*lt1k/total:>5.1f}%)")
        print(f"  diff 1k–10k kr             : {lt10k:>7,}  ({100*lt10k/total:>5.1f}%)")
        print(f"  diff >= 10k kr             : {ge10k:>7,}  ({100*ge10k/total:>5.1f}%)")
        print(f"  bara-i-DB (Mercur saknar)  : {only_db:>7,}")
        print(f"  bara-i-Mercur (DB saknar)  : {only_m:>7,}")

        print()
        print("=== Per land ===")
        cur = con.execute("""
            WITH joined AS (
                SELECT
                    COALESCE(m.company_id, d.company_id) AS company_id,
                    COALESCE(m.amount, 0) - COALESCE(d.amount, 0) AS diff
                FROM m_ytd m
                FULL OUTER JOIN db_ytd d
                  ON m.company_id = d.company_id
                 AND m.period = d.period
                 AND m.account_code = d.account_code
            )
            SELECT dc.country,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE ABS(diff) < 1.0) AS ok,
                   COUNT(*) FILTER (WHERE ABS(diff) >= 10000) AS ge10k_n,
                   ROUND(SUM(ABS(diff))::numeric,0) AS sum_abs_diff
            FROM joined j
            JOIN dim_company dc ON dc.company_id = j.company_id
            GROUP BY dc.country
            ORDER BY total DESC
        """)
        print(f'{"Land":<12} {"total":>8} {"ok":>8} {"ok%":>6} {">=10k":>7} {"abs(diff) sum":>14}')
        for row in cur.fetchall():
            country, t, ok_n, ge10k_n, abs_diff = row
            country = country or '?'
            t = t or 0
            ok_n = ok_n or 0
            ge10k_n = ge10k_n or 0
            abs_diff = float(abs_diff or 0)
            pct = 100*ok_n/t if t else 0
            print(f'{country:<12} {t:>8,} {ok_n:>8,} {pct:>5.1f}% {ge10k_n:>7,} {abs_diff:>14,.0f}')


if __name__ == "__main__":
    main()
