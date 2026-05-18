-- Per-konto-diff mellan backup_from_mercur (Mercur-facit) och fact_balances
-- (laddat data) för en specifik (company_id, period, source_kind, scenario='A').
--
-- Används av /api/compare/coverage/accounts som drilldown-data från
-- täckningssidans matris.
--
-- Parametrar (%%s × 3):
--   1: company_id   (int)
--   2: period       (text, YYYYMM)
--   3: source_kind  (text, en av: IMP, SIE, SAFT, MAN, IMP_ADJ)
--
-- För SIE/SAFT:
--   - Sign-flippa backup (Mercur-konvention → SIE-konvention) innan YTD-cum.
--   - YTD-kumulera SUM(-backup.amount) över jan..period inom samma år.
--   - Jämför mot fact_balances (YTD-saldo, SIE-konvention).
--   - BS-konton (1xxx, 2xxx) klassas som 'no_baseline' eftersom YTD-cum av
--     monthly-bevegelser saknar IB (ingående balans). Visas informativt i UI.
-- För IMP/MAN/IMP_ADJ:
--   - Jämför monthly↔monthly rakt av (båda sidor Mercur-konvention, samma plan).
--   - Alla status kan vara meningsfulla mismatch-tecken.
--
-- status_acc:
--   'ok'           |diff| ≤ max(1.0, 1%% × |facit_amt|)
--   'amount_diff'  båda finns, |diff| över tröskel
--   'only_facit'   bara backup har raden
--   'only_fact'    bara fact_balances har raden
--   'no_baseline'  BS-konto i SIE/SAFT — kan inte jämföras utan IB
WITH
sie_pick AS (
    SELECT DISTINCT company_id, period, scenario,
           FIRST_VALUE(source_kind) OVER (
               PARTITION BY company_id, period, scenario
               ORDER BY CASE source_kind WHEN 'SIE' THEN 1
                                          WHEN 'SIE_PSALDO' THEN 2 END
           ) AS picked_kind
    FROM fact_balances
    WHERE source_kind IN ('SIE', 'SIE_PSALDO') AND scenario = 'A'
),
account_diff_ytd AS (
    -- SIE/SAFT-grenen: YTD-kumulera SIGN-FLIPPAD backup, FULL JOIN mot YTD-fact.
    SELECT
        COALESCE(bk.company_id, fk.company_id)     AS company_id,
        COALESCE(bk.period,     fk.period)         AS period,
        COALESCE(bk.source_kind, fk.source_kind)   AS source_kind,
        COALESCE(bk.scenario,   fk.scenario)       AS scenario,
        COALESCE(bk.account_code, fk.account_code) AS account_code,
        fk.account_name,
        bk.facit_amt,
        fk.fact_amt,
        ROUND((COALESCE(bk.facit_amt, 0) - COALESCE(fk.fact_amt, 0))::numeric, 2) AS diff,
        -- BS-flagga används av status-CASE nedan för att markera no_baseline.
        LEFT(COALESCE(bk.account_code, fk.account_code), 1) IN ('1','2') AS is_bs
    FROM (
        SELECT company_id, period, source_kind, scenario, account_code,
               -- SIGN-FLIP: Mercur-konvention → SIE-konvention via -amount.
               SUM(-amount) OVER (
                   PARTITION BY company_id, LEFT(period, 4), source_kind, scenario, account_code
                   ORDER BY period
                   ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
               ) AS facit_amt
        FROM backup_from_mercur
        WHERE scenario = 'A' AND source_kind IN ('SIE', 'SAFT')
    ) bk
    FULL OUTER JOIN (
        SELECT fb.company_id, fb.period, 'SAFT' AS source_kind, fb.scenario,
               fb.account_code, fb.account_name, fb.amount AS fact_amt
        FROM fact_balances fb
        WHERE fb.scenario = 'A' AND fb.source_kind = 'SAFT'
        UNION ALL
        SELECT fb.company_id, fb.period, 'SIE' AS source_kind, fb.scenario,
               fb.account_code, fb.account_name, fb.amount AS fact_amt
        FROM fact_balances fb
        JOIN sie_pick p
          ON p.company_id = fb.company_id AND p.period = fb.period
         AND p.scenario   = fb.scenario   AND p.picked_kind = fb.source_kind
        WHERE fb.scenario = 'A'
    ) fk
      ON  bk.company_id   = fk.company_id
      AND bk.period       = fk.period
      AND bk.source_kind  = fk.source_kind
      AND bk.scenario     = fk.scenario
      AND bk.account_code = fk.account_code
),
account_diff_monthly AS (
    -- IMP/MAN/IMP_ADJ-grenen: monthly↔monthly rakt av, ingen sign-flip.
    -- is_bs=FALSE eftersom BS-jämförelse på monthly-rörelser är meaningful här.
    SELECT
        COALESCE(bk.company_id, fk.company_id)     AS company_id,
        COALESCE(bk.period,     fk.period)         AS period,
        COALESCE(bk.source_kind, fk.source_kind)   AS source_kind,
        COALESCE(bk.scenario,   fk.scenario)       AS scenario,
        COALESCE(bk.account_code, fk.account_code) AS account_code,
        fk.account_name,
        bk.amount AS facit_amt,
        fk.amount AS fact_amt,
        ROUND((COALESCE(bk.amount, 0) - COALESCE(fk.amount, 0))::numeric, 2) AS diff,
        FALSE AS is_bs
    FROM (
        SELECT company_id, period, source_kind, scenario, account_code, amount
        FROM backup_from_mercur
        WHERE scenario = 'A' AND source_kind IN ('IMP', 'MAN', 'IMP_ADJ')
    ) bk
    FULL OUTER JOIN (
        SELECT company_id, period, source_kind, scenario, account_code, account_name, amount
        FROM fact_balances
        WHERE scenario = 'A' AND source_kind IN ('IMP', 'MAN', 'IMP_ADJ')
    ) fk USING (company_id, period, source_kind, scenario, account_code)
),
account_diff AS (
    SELECT *,
           CASE
               -- BS-konton för SIE/SAFT: kan inte jämföras meaningfully utan IB.
               WHEN is_bs AND source_kind IN ('SIE', 'SAFT') THEN 'no_baseline'
               WHEN facit_amt IS NULL THEN 'only_fact'
               WHEN fact_amt  IS NULL THEN 'only_facit'
               WHEN ABS(diff) > GREATEST(1.0, 0.01 * ABS(facit_amt)) THEN 'amount_diff'
               ELSE 'ok'
           END AS status_acc
    FROM (
        SELECT * FROM account_diff_ytd
        UNION ALL
        SELECT * FROM account_diff_monthly
    ) merged
)
SELECT
    account_code,
    account_name,
    facit_amt,
    fact_amt,
    diff,
    status_acc
FROM account_diff
WHERE company_id  = %s
  AND period      = %s
  AND source_kind = %s
ORDER BY
    -- Sortordning: fel-status först, no_baseline informativt, ok sist.
    CASE status_acc
        WHEN 'amount_diff' THEN 0
        WHEN 'only_facit'  THEN 1
        WHEN 'only_fact'   THEN 2
        WHEN 'no_baseline' THEN 3
        ELSE 4
    END,
    ABS(diff) DESC NULLS LAST,
    account_code;
