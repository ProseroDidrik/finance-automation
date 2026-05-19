-- Per-konto-diff mellan backup_from_mercur (Mercur-facit) och fact_balances
-- (laddat data) för en specifik (company_id, period, source_kind, scenario='A').
--
-- Används av /api/compare/coverage/accounts som drilldown-data från
-- täckningssidans matris.
--
-- Spec: docs/superpowers/specs/2026-05-17-coverage-quality-design.md.
-- Empirically discovered constraints i Addendum A1/A2/A3 motiverar sign-flip
-- för SIE/SAFT, no_baseline-status för BS-konton, och grövre Mercur-kontoplan
-- som förklarar varför only_fact är förväntat för SIE/SAFT.
--
-- Tröskel per konto: |diff| > 1 OCH > 0.01*|facit|. OBS: konstanten är
-- duplicerad här (i status-CASE och i `account_diff_*`-CTE:rnas filter) och
-- i compare_coverage.sql (i account_diff:s WHERE). Vid ändring — uppdatera
-- alla fyra ställen, annars klassas matris och drilldown olika.
--
-- Logiken här delas med compare_coverage.sql men uttrycks i OLIKA SQL-form
-- (tre separata CTE:r här vs en inlinead här). En A2/A3-regeländring kräver
-- översättning mellan båda formerna, inte en find-replace.
--
-- Parametrar (%%s × 3 — OBS dubbel-%% i kommentarer eftersom psycopg scannar
-- HELA SQL-strängen för %%-placeholders inklusive comment-rader. Find-replace
-- av %%%% → %% bryter queryn silent vid runtime):
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
               -- SIGN-FLIP (spec A1): backup_from_mercur lagrar SIE/SAFT i
               -- Mercur-konvention (intäkt+, kostnad-), fact_balances i
               -- SIE-konvention (intäkt-, kostnad+). IMP/MAN/IMP_ADJ-grenen
               -- nedan flippar INTE — båda sidor är Mercur-konvention där.
               SUM(-monthly_amt) OVER (
                   PARTITION BY company_id, LEFT(period, 4), source_kind, scenario, account_code
                   ORDER BY period
                   ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
               ) AS facit_amt
        FROM (
            -- DEDUP före YTD-cum: backup_from_mercur har empiriskt flera
            -- fysiska rader per (bolag, period, källa, scenario, konto) för
            -- CENTR-bolag i `_history/2026 Backup.txt` (sannolikt dimensioner
            -- som load_history_excel.py inte aggregerat ihop). Utan denna
            -- SUM ger window-funktionen samma cum-värde för varje fysisk rad,
            -- och FULL OUTER JOIN nedan dupliserar konton i drilldownen.
            SELECT company_id, period, source_kind, scenario, account_code,
                   SUM(amount) AS monthly_amt
            FROM backup_from_mercur
            WHERE scenario = 'A' AND source_kind IN ('SIE', 'SAFT')
            GROUP BY 1, 2, 3, 4, 5
        ) bm
    ) bk
    FULL OUTER JOIN (
        -- DEDUP fact-sidan: fact_balances har empiriskt verifierat flera fysiska
        -- rader per (bolag, period, källa, scenario, konto) för SIE/SIE_PSALDO —
        -- load_sie.py aggregerar inte bort dimensioner/objektkoder från #RES/
        -- #PSALDO-rader, så samma konto kan finnas N gånger (en per kostnads-
        -- ställe). Utan SUM får backup en rad × N fact-rader i FULL OUTER JOIN
        -- och både per-konto-diff och status_acc blir fel. SAFT-grenen har inga
        -- dubbletter idag men aggregeras defensivt för symmetri.
        SELECT company_id, period, 'SAFT' AS source_kind, scenario,
               account_code, MAX(account_name) AS account_name,
               SUM(amount) AS fact_amt
        FROM fact_balances
        WHERE scenario = 'A' AND source_kind = 'SAFT'
        GROUP BY company_id, period, scenario, account_code
        UNION ALL
        SELECT fb.company_id, fb.period, 'SIE' AS source_kind, fb.scenario,
               fb.account_code, MAX(fb.account_name) AS account_name,
               SUM(fb.amount) AS fact_amt
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
        -- DEDUP: parallell till YTD-grenens dedup ovan. Inga dubbletter idag
        -- för IMP/MAN/IMP_ADJ men SUM-aggregeringen är defensiv mot framtida
        -- load_history-körningar och kostar inget när data redan är unik.
        SELECT company_id, period, source_kind, scenario, account_code,
               SUM(amount) AS amount
        FROM backup_from_mercur
        WHERE scenario = 'A' AND source_kind IN ('IMP', 'MAN', 'IMP_ADJ')
        GROUP BY 1, 2, 3, 4, 5
    ) bk
    FULL OUTER JOIN (
        -- DEDUP fact-sidan: defensiv mot samma dimensions-bug som YTD-grenen.
        -- IMP/MAN/IMP_ADJ har inga dubbletter idag men SUM-aggregeringen är
        -- en no-op när data är unik och försvar mot framtida load-körningar.
        SELECT company_id, period, source_kind, scenario, account_code,
               MAX(account_name) AS account_name,
               SUM(amount) AS amount
        FROM fact_balances
        WHERE scenario = 'A' AND source_kind IN ('IMP', 'MAN', 'IMP_ADJ')
        GROUP BY 1, 2, 3, 4, 5
    ) fk USING (company_id, period, source_kind, scenario, account_code)
),
account_diff AS (
    SELECT *,
           CASE
               -- no_baseline checkas FÖRE only_fact/only_facit (spec A3):
               -- ett BS-konto i SIE/SAFT som saknas på en sida ska ändå
               -- klassas no_baseline, inte only_*, eftersom avsaknad av IB
               -- gör hela BS-jämförelsen meningslös för SIE/SAFT.
               WHEN is_bs AND source_kind IN ('SIE', 'SAFT') THEN 'no_baseline'
               -- Tomma fact-rader (amount ~0) som inte finns i backup: Mercur
               -- skippar 0-belopps-konton i sin export medan SIE/SAFT-filer
               -- behåller hela kontoplanen. Empiriskt 2026-05-19: 99 %% av
               -- only_fact-rader i 202604 hade amount=0. Räkna dem som 'ok'
               -- så drilldownen inte druknar i tomma konton. OBS: %%-tecken
               -- måste dubblas (psycopg scannar hela SQL för %%-placeholders).
               WHEN facit_amt IS NULL AND ABS(COALESCE(fact_amt, 0)) < 1 THEN 'ok'
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
