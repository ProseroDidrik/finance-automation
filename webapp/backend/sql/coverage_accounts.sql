-- Per-konto-diff (drilldown fran tackningsmatrisen) for en (company_id,
-- period, source_kind, scenario='A') -- MANADSBASIS.
--
-- Modell: manadsrorelse mot manadsrorelse, ingen YTD-kumulering. Samma modell
-- som compare_coverage.sql (matrisen) -- en regelandring kraver oversattning
-- till bada filerna, inte en find-replace.
--   backup -> backup_from_mercur (sign-flippad for SIE/SAFT -> SIE-konvention)
--   fact   -> SIE: fact_journal_sie, SAFT: fact_journal_saft (deduppat per
--             senast laddade source_file), IMP/MAN/IMP_ADJ: fact_balances
--
-- Tre positionsplaceholders, anvanda en gang var via params-CTE:n:
--   1: company_id   (int)
--   2: period       (text, YYYYMM)
--   3: source_kind  (text: IMP, SIE, SAFT, MAN, IMP_ADJ)
-- Inga andra literala procent-tecken far forekomma (psycopg scannar hela
-- strangen, aven kommentarer, efter placeholders).
--
-- status_acc:
--   'ok'           |diff| <= GREATEST(1.0, 0.01 * |facit_amt|), eller tom
--                  only_fact-rad (|fact| < 1 -- Mercur skippar noll-konton)
--   'amount_diff'  bada finns, |diff| over troskeln
--   'only_facit'   bara backup har raden
--   'only_fact'    bara fact har raden (vantat for SIE/SAFT -- grovre
--                  Mercur-kontoplan -- men visas informativt)
WITH params AS (
    SELECT %s::int AS company_id, %s::text AS period, %s::text AS source_kind
),
-- BACKUP-sidan: manadsrorelse per konto, sign-flippad for SIE/SAFT.
backup_acct AS (
    SELECT b.account_code,
           SUM(b.amount * CASE WHEN b.source_kind IN ('SIE', 'SAFT')
                               THEN -1 ELSE 1 END) AS facit_amt
    FROM backup_from_mercur b
    JOIN params p
      ON b.company_id  = p.company_id
     AND b.period      = p.period
     AND b.source_kind = p.source_kind
    WHERE b.scenario = 'A'
    GROUP BY 1
),
-- Norska SAF-T-filer ar YTD: samma manads verifikat ligger i flera filer.
-- Valj senast laddade fil for (bolag, period). Tom nar source_kind <> 'SAFT'.
saft_pick AS (
    SELECT j.source_file
    -- T9 + T3.c: public.fact_journal_saft via column-grants (snabbare än vyn).
    FROM public.fact_journal_saft j
    JOIN params p ON j.company_id = p.company_id AND j.period = p.period
    WHERE p.source_kind = 'SAFT'
    ORDER BY j.loaded_at DESC
    LIMIT 1
),
-- FACT-sidan: manadsrorelse per konto fran ratt kalla beroende pa source_kind.
fact_acct AS (
    SELECT j.account_code,
           MAX(j.account_name) AS account_name,
           SUM(j.amount)       AS fact_amt
    -- T9 + T3.c: public.fact_journal_sie via column-grants.
    FROM public.fact_journal_sie j
    JOIN params p ON j.company_id = p.company_id AND j.period = p.period
    WHERE p.source_kind = 'SIE'
    GROUP BY 1
    UNION ALL
    SELECT j.account_code,
           NULL::text    AS account_name,
           SUM(j.amount) AS fact_amt
    -- T9 + T3.c: public.fact_journal_saft via column-grants (snabbare än vyn).
    FROM public.fact_journal_saft j
    JOIN params p ON j.company_id = p.company_id AND j.period = p.period
    WHERE p.source_kind = 'SAFT'
      AND j.source_file = (SELECT source_file FROM saft_pick)
    GROUP BY 1
    UNION ALL
    SELECT fb.account_code,
           MAX(fb.account_name) AS account_name,
           SUM(fb.amount)       AS fact_amt
    FROM fact_balances fb
    JOIN params p
      ON fb.company_id  = p.company_id
     AND fb.period      = p.period
     AND fb.source_kind = p.source_kind
    WHERE fb.scenario = 'A'
      AND p.source_kind IN ('IMP', 'MAN', 'IMP_ADJ')
    GROUP BY 1
),
-- Kontonamn for SAFT: fact_journal_saft saknar account_name -- hamta fran
-- SAF-T-saldoposterna i fact_balances (bolagsvitt, valfri period).
saft_name AS (
    SELECT fb.account_code, MAX(fb.account_name) AS account_name
    FROM fact_balances fb
    JOIN params p ON fb.company_id = p.company_id
    WHERE fb.source_kind = 'SAFT' AND p.source_kind = 'SAFT'
    GROUP BY 1
),
account_diff AS (
    SELECT
        bf.account_code,
        COALESCE(bf.account_name, sn.account_name) AS account_name,
        bf.facit_amt,
        bf.fact_amt,
        ROUND((COALESCE(bf.facit_amt, 0) - COALESCE(bf.fact_amt, 0))::numeric, 2) AS diff
    FROM (
        SELECT COALESCE(b.account_code, f.account_code) AS account_code,
               f.account_name,
               b.facit_amt,
               f.fact_amt
        FROM backup_acct b
        FULL OUTER JOIN fact_acct f USING (account_code)
    ) bf
    LEFT JOIN saft_name sn ON sn.account_code = bf.account_code
)
SELECT
    account_code,
    account_name,
    facit_amt,
    fact_amt,
    diff,
    CASE
        WHEN facit_amt IS NULL AND ABS(COALESCE(fact_amt, 0)) < 1 THEN 'ok'
        WHEN facit_amt IS NULL THEN 'only_fact'
        WHEN fact_amt  IS NULL THEN 'only_facit'
        WHEN ABS(diff) > GREATEST(1.0, 0.01 * ABS(facit_amt)) THEN 'amount_diff'
        ELSE 'ok'
    END AS status_acc
FROM account_diff
ORDER BY
    CASE
        WHEN facit_amt IS NULL AND ABS(COALESCE(fact_amt, 0)) < 1 THEN 3
        WHEN facit_amt IS NULL THEN 2
        WHEN fact_amt  IS NULL THEN 1
        WHEN ABS(diff) > GREATEST(1.0, 0.01 * ABS(facit_amt)) THEN 0
        ELSE 3
    END,
    ABS(diff) DESC NULLS LAST,
    account_code
