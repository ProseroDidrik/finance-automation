-- Jämförelse Mercur-facit (`backup_from_mercur`) vs `fact_balances` per
-- (bolag, period, källa, scenario).
--
-- Status:
--   'missing'      — facit har rader, fact_balances har inga (riktigt saknad data)
--   'missing_zero' — facit har rader men summan ≈ 0 för SIE/SAFT (Mercur har
--                    pre-allokerat tomma noll-rader för bolag utan månadsbevegelse;
--                    ingen riktig data saknas, bara harmlös pre-allokering)
--   'extra'        — fact_balances har rader, facit har inga (utanför facit-scope)
--   'mismatch'     — minst ett konto avviker (per-konto-test via account_diff-CTE,
--                    se nedan). För SIE/SAFT YTD-kumuleras backup med sign-flip
--                    (Mercur→SIE-konvention) innan jämförelse. BS-konton i SIE/SAFT
--                    klassas no_baseline (kan inte jämföras utan IB) och triggar
--                    INTE mismatch. only_fact triggar mismatch BARA för IMP/MAN/
--                    IMP_ADJ (samma kontoplan); för SIE/SAFT är only_fact förväntat
--                    (Mercur har grövre kontoplan än SIE-filen).
--   'ok'           — båda har rader, alla konton stämmer
--
-- account_diff-CTE:n delar logik med coverage_accounts.sql (drilldown-endpoint)
-- men de uttrycks i OLIKA SQL-form: här som en filtrerad inline-CTE (bara
-- felstatus exponeras till EXISTS), där som tre separata CTE:r med status_acc
-- i en CASE-gren. En ändring av A2/A3-reglerna kräver översättning mellan
-- båda formerna, inte en find-replace.
--
-- Tröskel per konto: |diff| > 1 OCH > 0.01*|facit|. OBS: konstanten är
-- duplicerad här (2× i WHERE) och i coverage_accounts.sql (2×). Vid ändring
-- — uppdatera alla fyra ställen, annars klassas matris och drilldown olika.
--
-- Empiriska constraints dokumenterade i spec-addendum A1-A3 i
-- docs/superpowers/specs/2026-05-17-coverage-quality-design.md.
--
-- Normalisering: fact_balances har SE-data som både SIE (transaktioner) och
-- SIE_PSALDO (periodsaldon från samma fil). Vi väljer SIE om den finns,
-- annars SIE_PSALDO. På så vis matchar backup.SIE direkt mot fact-SIE.
-- Begränsa till utfall (scenario='A') — budget (B) ligger utanför facit.
WITH cutoff AS (
    -- Täckningsmatrisen ska inte visa framtida bokföringsperioder. Mercur-
    -- backup innehåller framtida data både som ETL-rader (202605-SIE från
    -- SIE-filer som täcker hela året) och MAN-budget-prognoser fram till
    -- årets slut. Inget av detta ska klassas som 'missing' bara för att
    -- fact inte har laddats för den månaden än.
    --
    -- Cutoff = föregående kalendermånad. I maj 2026 → '202604'. Beräknas
    -- automatiskt via now() så koden inte behöver uppdateras månadsvis.
    -- Per-bolag-source-kind-cutoff skulle vara mer korrekt (om något bolag
    -- har laddat maj-fact medan andra ligger kvar i april) men kräver att
    -- vi särskiljer "saknad fact" från "fact-laddning ej körd än" per
    -- källa — komplexitet vi inte behöver idag.
    SELECT to_char(date_trunc('month', now()) - interval '1 month', 'YYYYMM') AS p
),
backup_agg AS (
    SELECT company_id, period, source_kind, scenario,
           COUNT(*)    AS rows,
           SUM(amount) AS total
    FROM backup_from_mercur
    WHERE scenario = 'A'
      AND period <= (SELECT p FROM cutoff)
    GROUP BY 1, 2, 3, 4
),
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
fact_sie AS (
    SELECT fb.company_id, fb.period, 'SIE' AS source_kind, fb.scenario,
           COUNT(*)::int  AS rows,
           SUM(fb.amount) AS total
    FROM sie_pick p
    JOIN fact_balances fb
      ON fb.company_id  = p.company_id
     AND fb.period      = p.period
     AND fb.scenario    = p.scenario
     AND fb.source_kind = p.picked_kind
    GROUP BY fb.company_id, fb.period, fb.scenario
),
fact_other AS (
    SELECT company_id, period, source_kind, scenario,
           COUNT(*)::int AS rows,
           SUM(amount)   AS total
    FROM fact_balances
    WHERE source_kind IN ('IMP', 'SAFT', 'MAN', 'IMP_ADJ') AND scenario = 'A'
    GROUP BY 1, 2, 3, 4
),
fact_agg AS (
    SELECT * FROM fact_sie
    UNION ALL
    SELECT * FROM fact_other
),
-- Per-konto-diff (delad logik med coverage_accounts.sql). UNION ALL av
-- YTD-grenen (SIE/SAFT med sign-flip) och monthly-grenen (IMP/MAN/IMP_ADJ).
-- Förfiltrerat så bara felstatusar exponeras till EXISTS-testet — sparar arbete.
account_diff AS (
    SELECT * FROM (
        -- YTD-gren: SIE/SAFT — sign-flippa Mercur-konvention och YTD-cum:a.
        SELECT
            COALESCE(bk.company_id, fk.company_id)     AS company_id,
            COALESCE(bk.period,     fk.period)         AS period,
            COALESCE(bk.source_kind, fk.source_kind)   AS source_kind,
            COALESCE(bk.scenario,   fk.scenario)       AS scenario,
            COALESCE(bk.account_code, fk.account_code) AS account_code,
            bk.facit_amt,
            fk.fact_amt,
            ROUND((COALESCE(bk.facit_amt, 0) - COALESCE(fk.fact_amt, 0))::numeric, 2) AS diff,
            LEFT(COALESCE(bk.account_code, fk.account_code), 1) IN ('1','2') AS is_bs
        FROM (
            SELECT company_id, period, source_kind, scenario, account_code,
                   SUM(-monthly_amt) OVER (
                       PARTITION BY company_id, LEFT(period, 4), source_kind, scenario, account_code
                       ORDER BY period
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                   ) AS facit_amt
            FROM (
                -- DEDUP före YTD-cum: spegel av coverage_accounts.sql:s
                -- backup_ytd-CTE. backup_from_mercur har empiriskt flera
                -- fysiska rader per (bolag, period, källa, scenario, konto)
                -- för CENTR-bolag i `_history/2026 Backup.txt`. Utan denna
                -- SUM duplikerar window-funktionen + FULL OUTER JOIN raderna
                -- och account_diff EXISTS-testet kan trigga falska mismatch.
                SELECT company_id, period, source_kind, scenario,
                       account_code, SUM(amount) AS monthly_amt
                FROM backup_from_mercur
                WHERE scenario = 'A' AND source_kind IN ('SIE', 'SAFT')
                  AND period <= (SELECT p FROM cutoff)
                GROUP BY 1, 2, 3, 4, 5
            ) bm
        ) bk
        FULL OUTER JOIN (
            -- DEDUP fact-sidan: spegel av coverage_accounts.sql:s fact-dedup.
            -- fact_balances har empiriskt flera fysiska rader per
            -- (bolag, period, källa, scenario, konto) för SIE/SIE_PSALDO
            -- (load_sie.py aggregerar inte bort dimensioner från #RES/#PSALDO).
            -- Utan SUM duplicerar FULL OUTER JOIN raderna och account_diff
            -- EXISTS-testet kan trigga falska mismatch.
            SELECT company_id, period, 'SAFT' AS source_kind, scenario,
                   account_code, SUM(amount) AS fact_amt
            FROM fact_balances
            WHERE scenario = 'A' AND source_kind = 'SAFT'
            GROUP BY company_id, period, scenario, account_code
            UNION ALL
            SELECT fb.company_id, fb.period, 'SIE' AS source_kind, fb.scenario,
                   fb.account_code, SUM(fb.amount) AS fact_amt
            FROM fact_balances fb
            JOIN sie_pick p
              ON p.company_id = fb.company_id AND p.period = fb.period
             AND p.scenario   = fb.scenario   AND p.picked_kind = fb.source_kind
            WHERE fb.scenario = 'A'
            GROUP BY fb.company_id, fb.period, fb.scenario, fb.account_code
        ) fk
          ON  bk.company_id   = fk.company_id
          AND bk.period       = fk.period
          AND bk.source_kind  = fk.source_kind
          AND bk.scenario     = fk.scenario
          AND bk.account_code = fk.account_code

        UNION ALL

        -- Monthly-gren: IMP/MAN/IMP_ADJ — monthly↔monthly rakt av, ingen sign-flip.
        -- is_bs=FALSE eftersom BS-jämförelse på monthly-rörelser är meaningful här.
        SELECT
            COALESCE(bk.company_id, fk.company_id)     AS company_id,
            COALESCE(bk.period,     fk.period)         AS period,
            COALESCE(bk.source_kind, fk.source_kind)   AS source_kind,
            COALESCE(bk.scenario,   fk.scenario)       AS scenario,
            COALESCE(bk.account_code, fk.account_code) AS account_code,
            bk.amount AS facit_amt,
            fk.amount AS fact_amt,
            ROUND((COALESCE(bk.amount, 0) - COALESCE(fk.amount, 0))::numeric, 2) AS diff,
            FALSE AS is_bs
        FROM (
            -- DEDUP: parallell till YTD-grenens dedup ovan. Defensiv mot
            -- framtida load_history-körningar; backup_from_mercur har inga
            -- dubbletter för IMP/MAN/IMP_ADJ idag och SUM-aggregeringen är
            -- en no-op när data redan är unik.
            SELECT company_id, period, source_kind, scenario,
                   account_code, SUM(amount) AS amount
            FROM backup_from_mercur
            WHERE scenario = 'A' AND source_kind IN ('IMP', 'MAN', 'IMP_ADJ')
              AND period <= (SELECT p FROM cutoff)
            GROUP BY 1, 2, 3, 4, 5
        ) bk
        FULL OUTER JOIN (
            -- DEDUP fact-sidan: defensiv parallell till YTD-grenens dedup.
            -- IMP/MAN/IMP_ADJ har inga dubbletter idag; SUM är no-op när
            -- data är unik och försvar mot framtida load-buggar.
            SELECT company_id, period, source_kind, scenario, account_code,
                   SUM(amount) AS amount
            FROM fact_balances
            WHERE scenario = 'A' AND source_kind IN ('IMP', 'MAN', 'IMP_ADJ')
            GROUP BY 1, 2, 3, 4, 5
        ) fk USING (company_id, period, source_kind, scenario, account_code)
    ) merged
    WHERE
        -- BS i SIE/SAFT (no_baseline) räknas INTE som mismatch.
        NOT (is_bs AND source_kind IN ('SIE', 'SAFT'))
        AND (
            -- IMP/MAN/IMP_ADJ: alla felstatusar räknas — men tomma fact-rader
            -- (amount ~0) exkluderas eftersom Mercur skippar 0-belopps-konton
            -- i sin export medan SIE/SAFT-filer behåller hela kontoplanen
            -- (empirisk 2026-05-19: 99 %% av only_fact-rader är tomma).
            -- OBS: %%-tecken måste dubblas (psycopg %%s-placeholder-scan).
            (source_kind IN ('IMP', 'MAN', 'IMP_ADJ')
             AND (
                (facit_amt IS NULL AND ABS(COALESCE(fact_amt, 0)) >= 1)
                OR fact_amt IS NULL
                OR ABS(ROUND((COALESCE(facit_amt,0) - COALESCE(fact_amt,0))::numeric, 2))
                     > GREATEST(1.0, 0.01 * ABS(COALESCE(facit_amt, 0)))
             ))
            -- SIE/SAFT: bara amount_diff + only_facit (only_fact är grövre plan)
            OR (source_kind IN ('SIE', 'SAFT')
                AND (
                    fact_amt IS NULL  -- only_facit
                    OR (facit_amt IS NOT NULL
                        AND ABS(ROUND((COALESCE(facit_amt,0) - COALESCE(fact_amt,0))::numeric, 2))
                             > GREATEST(1.0, 0.01 * ABS(facit_amt)))
                ))
        )
)
SELECT
    COALESCE(b.company_id,   f.company_id)   AS company_id,
    c.name                                    AS company_name,
    c.country,
    COALESCE(b.period,       f.period)        AS period,
    COALESCE(b.source_kind,  f.source_kind)   AS source_kind,
    COALESCE(b.scenario,     f.scenario)      AS scenario,
    b.rows   AS backup_rows,
    f.rows   AS fact_rows,
    b.total  AS backup_sum,
    f.total  AS fact_sum,
    CASE
        -- Pre-allokerade noll-rader i Mercur-backup för SIE/SAFT — harmlöst.
        -- IMP behåller "missing" trots bk_sum≈0 (IMP balanserar till 0 per design,
        -- så summan särskiljer inte tomma rader från riktigt saknade).
        WHEN f.company_id IS NULL
             AND b.source_kind IN ('SIE', 'SAFT')
             AND ABS(COALESCE(b.total, 0)) < 1
        THEN 'missing_zero'
        WHEN f.company_id IS NULL THEN 'missing'
        WHEN b.company_id IS NULL THEN 'extra'
        -- Per-konto-mismatch (gäller alla källor). account_diff är förfiltrerat
        -- så EXISTS bara returnerar något om minst ett konto faktiskt avviker
        -- (efter source-specifik filtrering — se account_diff-CTE:n ovan).
        WHEN EXISTS (
            SELECT 1 FROM account_diff ad
            WHERE ad.company_id  = COALESCE(b.company_id, f.company_id)
              AND ad.period      = COALESCE(b.period, f.period)
              AND ad.source_kind = COALESCE(b.source_kind, f.source_kind)
              AND ad.scenario    = COALESCE(b.scenario, f.scenario)
        ) THEN 'mismatch'
        ELSE 'ok'
    END AS status
FROM backup_agg b
FULL OUTER JOIN fact_agg f
    ON  b.company_id  = f.company_id
    AND b.period      = f.period
    AND b.source_kind = f.source_kind
    AND b.scenario    = f.scenario
LEFT JOIN dim_company c
    ON COALESCE(b.company_id, f.company_id) = c.company_id
ORDER BY
    COALESCE(b.period, f.period),
    c.country,
    COALESCE(b.company_id, f.company_id)
