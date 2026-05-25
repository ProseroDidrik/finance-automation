-- Tackningsmatris: Mercur-facit (backup_from_mercur) vs laddad data, jamfort
-- per (bolag, period, kalla, scenario) pa MANADSBASIS.
--
-- Modell: manadsrorelse mot manadsrorelse for alla kallslag. Ingen
-- YTD-kumulering. backup_from_mercur lagrar redan manadsrorelser. Fact-sidan
-- normaliseras ocksa till manadsrorelse:
--   SIE  -> fact_journal_sie   (SUM per konto+period = manadsrorelse)
--   SAFT -> fact_journal_saft  (dito; deduppat per source_file, se nedan)
--   IMP/MAN/IMP_ADJ -> fact_balances (redan manadsrorelse)
--
-- Varfor journal for SIE/SAFT: fact_balances lagrar dem som YTD-saldon. En
-- YTD-diff (ytd(M) - ytd(M-1)) spar i sig kalenderarsgranser och
-- #PSALDO/#RES-timingbrus. Verifikaten (journal) har faktiska
-- bokforingsdatum -> exakt manadsrorelse per konto, naturligt komplett aven
-- for bolag som bara levererar en arsfil (t.ex. Actas DK).
--
-- Teckenkonvention: backup_from_mercur lagrar SIE/SAFT i Mercur-konvention
-- (intakt +), fact i SIE-konvention (intakt -). Backup sign-flippas
-- (SUM(-amount)) for SIE/SAFT. IMP/MAN/IMP_ADJ ar Mercur-konvention pa bada
-- sidor -> ingen flip.
--
-- Status per cell:
--   missing      facit har rader, fact saknar
--   missing_zero facit har rader men summa ~0 for SIE/SAFT (Mercur
--                pre-allokerar tomma noll-rader for bolag utan rorelse)
--   extra        fact har rader, facit saknar
--   mismatch     minst ett konto avviker over troskeln
--   ok           bada finns, alla konton stammer
--
-- Per-konto-troskel: |diff| > GREATEST(1.0, 0.01 * |facit|). Delar logik med
-- coverage_accounts.sql (drilldown) men uttrycks dar i annan SQL-form (en
-- regelandring kraver oversattning till bada filerna, inte en find-replace).
--
-- Periodintervall: @period_lo@ / @period_hi@ ar platshallare som main.py
-- ersatter med 6-siffriga periodliteraler (regex-validerade -- ofarliga att
-- substituera). De MASTE vara literaler direkt i WHERE: en CTE-subquery eller
-- bind-param gor vardet ogenomskinligt for planeraren, som da underskattar
-- selektiviteten och valjer en disk-spillande sort i stallet for hash-aggregat
-- (~4x langsammare pa fact_journal_sie). main.py kapar @period_hi@ till
-- foregaende kalendermanad sa matrisen inte visar framtida perioder.
WITH
-- BACKUP-sidan: manadsrorelse per konto, sign-normaliserad till SIE-konvention
-- for SIE/SAFT. SUM aggregerar bort dubbelrader (CENTR-bolag har empiriskt
-- flera fysiska rader per konto i _history/2026 Backup.txt).
backup_acct AS (
    SELECT company_id, period, source_kind, scenario, account_code,
           SUM(amount * CASE WHEN source_kind IN ('SIE', 'SAFT')
                             THEN -1 ELSE 1 END) AS amount
    FROM backup_from_mercur
    WHERE scenario = 'A'
      AND period >= '@period_lo@' AND period <= '@period_hi@'
    GROUP BY 1, 2, 3, 4, 5
),
-- Norska SAF-T-filer ar YTD: varje manadsfil innehaller hela arets verifikat
-- hittills. load_saft.py:s idempotens ar per source_file, sa januari-verifikat
-- ligger samtidigt i jan/feb/mar/apr-filerna. saft_by_file aggregerar
-- verifikaten per (bolag, period, fil, konto) i ETT pass; saft_acct valjer
-- sedan senast laddade fil per (bolag, period, konto) sa varje manad raknas
-- exakt en gang. Bolag med en arsfil (Actas DK) eller manadsavgransade filer
-- paverkas inte.
saft_by_file AS (
    SELECT company_id, period, source_file, account_code,
           SUM(amount)    AS amount,
           MAX(loaded_at) AS loaded_at
    -- T9: byt till reporting.journal_saft (mcp_readonly saknar SELECT på public).
    -- Aggregat-fälten är oförändrade i vyn; bara line_description är maskad.
    FROM reporting.journal_saft
    WHERE period >= '@period_lo@' AND period <= '@period_hi@'
    GROUP BY 1, 2, 3, 4
),
saft_acct AS (
    SELECT DISTINCT ON (company_id, period, account_code)
           company_id, period, account_code, amount
    FROM saft_by_file
    ORDER BY company_id, period, account_code, loaded_at DESC
),
-- FACT-sidan: manadsrorelse per konto, normaliserad fran tre kallor.
fact_acct AS (
    SELECT company_id, period, 'SIE' AS source_kind, 'A' AS scenario,
           account_code, SUM(amount) AS amount
    -- T9: byt till reporting.journal_sie (samma motivering som ovan).
    FROM reporting.journal_sie
    WHERE period >= '@period_lo@' AND period <= '@period_hi@'
    GROUP BY 1, 2, 3, 4, 5
    UNION ALL
    SELECT company_id, period, 'SAFT' AS source_kind, 'A' AS scenario,
           account_code, amount
    FROM saft_acct
    UNION ALL
    SELECT company_id, period, source_kind, scenario,
           account_code, SUM(amount) AS amount
    FROM fact_balances
    WHERE scenario = 'A'
      AND source_kind IN ('IMP', 'MAN', 'IMP_ADJ')
      AND period >= '@period_lo@' AND period <= '@period_hi@'
    GROUP BY 1, 2, 3, 4, 5
),
-- Cell-niva: kontoantal + summa per (bolag, period, kalla, scenario).
backup_agg AS (
    SELECT company_id, period, source_kind, scenario,
           COUNT(*)::int AS rows,
           SUM(amount)   AS total
    FROM backup_acct
    GROUP BY 1, 2, 3, 4
),
fact_agg AS (
    SELECT company_id, period, source_kind, scenario,
           COUNT(*)::int AS rows,
           SUM(amount)   AS total
    FROM fact_acct
    GROUP BY 1, 2, 3, 4
),
-- Per-konto-diff. Forfiltrerat sa bara konton som faktiskt avviker over
-- troskeln exponeras till EXISTS-testet nedan -- haller det billigt.
account_diff AS (
    SELECT company_id, period, source_kind, scenario, account_code
    FROM (
        SELECT company_id, period, source_kind, scenario, account_code,
               b.amount AS facit_amt,
               f.amount AS fact_amt
        FROM backup_acct b
        FULL OUTER JOIN fact_acct f
            USING (company_id, period, source_kind, scenario, account_code)
    ) d
    WHERE
        -- only_facit: facit har kontot, fact saknar -- alltid mismatch.
        fact_amt IS NULL
        -- only_fact: fact har kontot, facit saknar. Mismatch bara for
        -- IMP/MAN/IMP_ADJ (samma kontoplan). For SIE/SAFT ar only_fact
        -- vantat -- Mercur har grovre kontoplan an SIE/SAF-T-filen.
        OR (facit_amt IS NULL
            AND source_kind IN ('IMP', 'MAN', 'IMP_ADJ')
            AND ABS(COALESCE(fact_amt, 0)) >= 1)
        -- amount_diff: bada finns, beloppen skiljer over troskeln.
        OR (facit_amt IS NOT NULL AND fact_amt IS NOT NULL
            AND ABS(ROUND((facit_amt - fact_amt)::numeric, 2))
                > GREATEST(1.0, 0.01 * ABS(facit_amt)))
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
        -- Pre-allokerade noll-rader i Mercur-backup for SIE/SAFT -- harmlost.
        WHEN f.company_id IS NULL
             AND b.source_kind IN ('SIE', 'SAFT')
             AND ABS(COALESCE(b.total, 0)) < 1
        THEN 'missing_zero'
        WHEN f.company_id IS NULL THEN 'missing'
        WHEN b.company_id IS NULL THEN 'extra'
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
