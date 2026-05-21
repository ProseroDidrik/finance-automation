-- report_pnl.sql
-- Returnerar P&L-trädet för (company_id, period) som en flat tabell.
-- En rad per nod i P&L-hierarkin: aggregerade noder (storgrupp/grupp/gruppkonto)
-- + bolagskonto-leaves (bara för det valda bolaget).
--
-- Amounts:
--   amount_month — månadens belopp (SE/NO: YTD - prev_month_YTD; IMP: rådata)
--   amount_ytd   — YTD-belopp t.o.m. perioden
-- Sign-konvention: SIE-rå (revenue negativ, expense positiv).
-- Presentation/sign-flip görs i frontend via pnl_kpis.yaml.
--
-- Utfall = vald BAS-källa (en av SIE*/SAFT/IMP) + alltid MAN + IMP_ADJ ovanpå.
-- MAN/IMP_ADJ på vanliga konton ligger i SIE-konvention, P-koder i Mercur-
-- konvention — raw_balances normaliserar båda (se rad-kommentar nedan).
--
-- KÄND BEGRÄNSNING: YTD-konvertering antar kalenderår (jan-dec). Bolag med
-- räkenskapsår som inte börjar i januari (t.ex. sept-aug) ger fel YTD-värden.
-- Alla nuvarande data följer kalenderår; dokumenterat för framtida laddningar.
--
-- Parametrar (positionsbundna, i den ordning platshallarna forekommer):
--   1:   source_kind override (NULL = auto via best_source)
--   2-4: company_id, year_start, period           — best_source
--   5-8: company_id, year_start, period, scenario — raw_balances
--   9:   prev_period   — balances 3a (SE/NO YTD-subtraktion)
--  10:   period        — balances 3a (valt period)
--  11:   period        — balances 3b (valda månadens rörelse)

WITH RECURSIVE
-- 1. Aggregerade noder i P&L-trädet.
pnl_tree(account_id, parent_id, label_sv, label_en, depth, sort_path) AS (
  SELECT account_id, parent_id, description, description_en, 0, account_id
  FROM dim_account_map WHERE account_id = 'P&L'
  UNION ALL
  SELECT m.account_id, m.parent_id, m.description, m.description_en,
         t.depth + 1, t.sort_path || '/' || m.account_id
  FROM dim_account_map m
  JOIN pnl_tree t ON m.parent_id = t.account_id
  WHERE m.is_aggregated = TRUE
),

-- 2a. Bästa tillgängliga BAS-källa per period — en mutuellt uteslutande
--     representation av huvudboken (prioritetsordning per land).
--     Endast bas-källor: SIE_PSALDO/SIE_VER/SIE/SAFT/IMP.
--     MAN och IMP_ADJ väljs ALDRIG här — de är additiva justeringslager som
--     alltid summeras ovanpå basen (se raw_balances OR-villkor nedan).
--     Saknas bas-källa (t.ex. bara IMP_ADJ) blir source_kind NULL; justerings-
--     lagret ger ändå output. Saknas allt → NULL → tom rapport.
best_source AS (
  SELECT
    fb.company_id,
    fb.period,
    -- Om explicit source_kind ges (param 4), använd den; annars välj per land via prioritet.
    COALESCE(%s, CASE c.country
      WHEN 'Sweden' THEN
        CASE
          WHEN MAX(CASE WHEN fb.source_kind = 'SIE_PSALDO' THEN 1 ELSE 0 END) = 1 THEN 'SIE_PSALDO'
          WHEN MAX(CASE WHEN fb.source_kind = 'SIE_VER'    THEN 1 ELSE 0 END) = 1 THEN 'SIE_VER'
          WHEN MAX(CASE WHEN fb.source_kind = 'SIE'        THEN 1 ELSE 0 END) = 1 THEN 'SIE'
          WHEN MAX(CASE WHEN fb.source_kind = 'IMP'        THEN 1 ELSE 0 END) = 1 THEN 'IMP'
          ELSE NULL
        END
      WHEN 'Norway' THEN
        CASE
          WHEN MAX(CASE WHEN fb.source_kind = 'SAFT'       THEN 1 ELSE 0 END) = 1 THEN 'SAFT'
          WHEN MAX(CASE WHEN fb.source_kind = 'SIE_PSALDO' THEN 1 ELSE 0 END) = 1 THEN 'SIE_PSALDO'
          WHEN MAX(CASE WHEN fb.source_kind = 'SIE'        THEN 1 ELSE 0 END) = 1 THEN 'SIE'
          WHEN MAX(CASE WHEN fb.source_kind = 'IMP'        THEN 1 ELSE 0 END) = 1 THEN 'IMP'
          ELSE NULL
        END
      WHEN 'CA' THEN
        CASE
          WHEN MAX(CASE WHEN fb.source_kind = 'SIE_PSALDO' THEN 1 ELSE 0 END) = 1 THEN 'SIE_PSALDO'
          WHEN MAX(CASE WHEN fb.source_kind = 'SIE_VER'    THEN 1 ELSE 0 END) = 1 THEN 'SIE_VER'
          WHEN MAX(CASE WHEN fb.source_kind = 'SIE'        THEN 1 ELSE 0 END) = 1 THEN 'SIE'
          WHEN MAX(CASE WHEN fb.source_kind = 'IMP'        THEN 1 ELSE 0 END) = 1 THEN 'IMP'
          ELSE NULL
        END
      ELSE  -- Finland, Denmark, Germany, CENTR
        CASE
          WHEN MAX(CASE WHEN fb.source_kind = 'IMP'     THEN 1 ELSE 0 END) = 1 THEN 'IMP'
          ELSE NULL
        END
    END) AS source_kind
  FROM fact_balances fb
  JOIN dim_company c ON c.company_id = fb.company_id
  WHERE fb.company_id = %s
    AND fb.period BETWEEN %s AND %s
  GROUP BY fb.company_id, fb.period, c.country
),

-- 2b. Hämta perioder från årsstart t.o.m. valt period (för IMP-kumulering)
--     + minst förra månaden (för SE/NO YTD-diff).
--
-- P-koder (t.ex. P_30, P_40) lagras av load_history_excel.py i Mercur-
-- presentationskonvention: intäkt positiv, kostnad negativ.
-- Alla andra konton följer SIE-konvention: intäkt negativ, kostnad positiv.
-- Vi normaliserar till SIE-konvention här så att resten av queryn fungerar lika.
--
-- Scenario-filter: NULL = alla scenarion summeras; 'A' = utfall; 'B' = budget.
-- GROUP BY summerar därefter ihop ev. dubbletter inom valt scenario per
-- (period, account_code) så varje rad är unik nedströms.
raw_balances AS (
  SELECT fb.company_id, fb.period, fb.account_code, fb.period_type,
         MAX(fb.account_name) AS account_name,
         SUM(fb.amount * CASE WHEN fb.account_code LIKE 'P_%%' THEN -1 ELSE 1 END) AS amount
  FROM fact_balances fb
  JOIN best_source bs
    ON bs.company_id  = fb.company_id
   AND bs.period      = fb.period
   -- Bas-källan (best_source) ELLER ett additivt justeringslager. MAN/IMP_ADJ
   -- summeras alltid ovanpå basen, oavsett vilken bas-källa som valts.
   AND (bs.source_kind = fb.source_kind
        OR fb.source_kind IN ('MAN', 'IMP_ADJ'))
  WHERE fb.company_id = %s
    AND fb.period BETWEEN %s AND %s
    AND fb.scenario = COALESCE(%s, fb.scenario)
  -- period_type med i GROUP BY: en YTD-bas-rad (SE/NO) och en monthly
  -- MAN/IMP_ADJ-rad för samma konto hålls isär (olika periodsemantik) och
  -- summeras först i leaf_amounts. För IMP-länder (monthly bas) hamnar bas +
  -- justering i samma grupp och summeras direkt — korrekt.
  GROUP BY fb.company_id, fb.period, fb.account_code, fb.period_type
),

-- 3. Normalisera till (amount_month, amount_ytd) per konto för vald period.
--    3a. YTD-källor (SIE/SAFT): en rad per konto; månad = YTD − föregående YTD.
--    3b. Monthly-källor (IMP/MAN/IMP_ADJ): en rad per konto med data i
--        jan..period; YTD = summa över månaderna, månad = valda månadens
--        rörelse (0 om ingen).
--    OBS: monthly-grenen grupperar över HELA jan..period utan period-filter.
--    MAN/IMP_ADJ är glesa — ett konto med en post i t.ex. januari men inte i
--    vald månad måste ändå räknas in i YTD-summan. Ett "WHERE period = valt"
--    skulle tappa den (buggen som filtrerade bort glesa justeringar).
balances AS (
  -- 3a. YTD-källor (SIE/SAFT)
  SELECT
    cur.company_id, cur.account_code, cur.account_name,
    cur.amount - COALESCE(prev.amount, 0) AS amount_month,
    cur.amount                            AS amount_ytd
  FROM raw_balances cur
  LEFT JOIN raw_balances prev
    ON prev.company_id   = cur.company_id
   AND prev.account_code = cur.account_code
   AND prev.period       = %s           -- prev_period
   AND prev.period_type  = 'ytd'
  WHERE cur.period = %s                  -- valt period
    AND cur.period_type = 'ytd'

  UNION ALL

  -- 3b. Monthly-källor (IMP/MAN/IMP_ADJ) — additiva, ev. glesa
  SELECT
    mb.company_id, mb.account_code,
    MAX(mb.account_name) AS account_name,
    SUM(CASE WHEN mb.period = %s THEN mb.amount ELSE 0 END) AS amount_month,
    SUM(mb.amount)                                         AS amount_ytd
  FROM raw_balances mb
  WHERE mb.period_type = 'monthly'
  GROUP BY mb.company_id, mb.account_code
),

-- 4. Bolagskonton som mappar in i P&L-trädet via parent_id.
--    Matchar antingen via (company_id, account_code) för vanliga konton,
--    eller via account_id direkt för P-koder (company_id=NULL, account_code=NULL).
leaf_amounts AS (
  -- Ett konto kan ha två balances-rader (YTD-bas + monthly MAN/IMP_ADJ för
  -- SE/NO) — summera ihop dem till en leaf-rad.
  SELECT
    m.account_id          AS leaf_node_id,
    m.parent_id           AS group_node_id,
    m.account_code,
    MAX(b.account_name)   AS leaf_label,
    SUM(b.amount_month)   AS amount_month,
    SUM(b.amount_ytd)     AS amount_ytd
  FROM balances b
  JOIN dim_account_map m
    ON  (m.company_id = b.company_id AND m.account_code = b.account_code)
     OR (m.account_id = b.account_code AND m.account_code IS NULL AND m.company_id IS NULL)
  JOIN pnl_tree t ON t.account_id = m.parent_id
  GROUP BY m.account_id, m.parent_id, m.account_code
),

-- 5. Walka uppåt från varje leaf, en rad per (leaf, ancestor) — för rollup.
ancestor_walk(leaf_node_id, ancestor_id, amount_month, amount_ytd) AS (
  SELECT leaf_node_id, group_node_id, amount_month, amount_ytd
  FROM leaf_amounts
  UNION ALL
  SELECT a.leaf_node_id, m.parent_id, a.amount_month, a.amount_ytd
  FROM ancestor_walk a
  JOIN dim_account_map m ON m.account_id = a.ancestor_id
  WHERE m.parent_id IS NOT NULL
),

-- 6. Summa per ancestor → amount för varje aggregerad nod.
agg_sums AS (
  SELECT ancestor_id,
         SUM(amount_month) AS amount_month,
         SUM(amount_ytd)   AS amount_ytd
  FROM ancestor_walk
  GROUP BY ancestor_id
)

-- 7a. Aggregerade rader (storgrupp / grupp / gruppkonto)
SELECT
  t.account_id,
  t.parent_id,
  t.label_sv,
  t.label_en,
  TRUE AS is_aggregated,
  t.depth,
  CAST(NULL AS VARCHAR) AS account_code,
  CAST(NULL AS VARCHAR) AS leaf_label,
  agg.amount_month,
  agg.amount_ytd,
  t.sort_path
FROM pnl_tree t
LEFT JOIN agg_sums agg ON agg.ancestor_id = t.account_id
WHERE t.account_id != 'P&L'

UNION ALL

-- 7b. Bolagskonto-leaves (för valt bolag)
SELECT
  m.account_id,
  m.parent_id,
  m.description AS label_sv,
  m.description_en AS label_en,
  FALSE AS is_aggregated,
  t.depth + 1 AS depth,
  m.account_code,
  l.leaf_label,
  l.amount_month,
  l.amount_ytd,
  t.sort_path || '/' || m.account_id AS sort_path
FROM leaf_amounts l
JOIN dim_account_map m ON m.account_id = l.leaf_node_id
JOIN pnl_tree t ON t.account_id = m.parent_id

ORDER BY sort_path
