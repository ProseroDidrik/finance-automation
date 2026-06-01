# SQL-mönster för YTD-aggregat — period_type-medveten

Detta är det viktigaste dokumentet att läsa innan du skriver SQL. Den enskilt vanligaste fällan är att behandla `SIE_PSALDO` som om det vore YTD när det är monthly.

## period_type per source_kind

Bekräftat empiriskt i `fact_balances`:

| source_kind | period_type | Logik för YTD-belopp |
|---|---|---|
| `SIE` | `ytd` | Ta `amount` vid period direkt |
| `SIE_VER` | `ytd` | Ta `amount` vid period direkt |
| `SAFT` | `ytd` | Ta `amount` vid period direkt |
| `SIE_PSALDO` | **`monthly`** | Summera `amount` jan..period |
| `IMP` | `monthly` | Summera `amount` jan..period |
| `MAN` | `monthly` | Summera `amount` jan..period (justerings­lager, additivt) |
| `IMP_ADJ` | `monthly` | Summera `amount` jan..period (justerings­lager, additivt) |
| `IB` | `monthly` | Ingående balans 202112, irrelevant för YTD |

## Korrekt YTD-CTE — copy-paste-bas

```sql
WITH RECURSIVE walk AS (
  -- För varje bolagskonto: hitta dess top_group via rekursiv parent-walk
  SELECT m.company_id, m.account_code, m.account_id AS cur_id, m.parent_id, 0 AS depth
  FROM dim_account_map m
  WHERE m.is_aggregated = FALSE AND m.company_id IS NOT NULL
  UNION ALL
  SELECT w.company_id, w.account_code, p.account_id, p.parent_id, w.depth + 1
  FROM walk w JOIN dim_account_map p ON w.parent_id = p.account_id
  WHERE w.depth < 10
),
acc_topgroup AS (
  SELECT DISTINCT ON (company_id, account_code) company_id, account_code, cur_id AS top_group
  FROM walk
  WHERE cur_id IN ('Total Sales','Total Direct Cost','Personnel','Consultants',
                   'Other External Costs','Premises','Transportation','Depreciation')
  ORDER BY company_id, account_code, depth DESC
),
fb_signed AS (
  -- All fact-data med tecken-flip för P_*-koder (Mercur P-koder är teckenflippade)
  SELECT fb.company_id, fb.period, fb.account_code, fb.source_kind,
         fb.amount * CASE WHEN fb.account_code LIKE 'P_%' THEN -1 ELSE 1 END AS amount
  FROM fact_balances fb
  WHERE fb.scenario='A'
    AND fb.source_kind IN ('SIE_PSALDO','SIE_VER','SIE','SAFT','IMP','MAN','IMP_ADJ')
    AND fb.period BETWEEN '202501' AND '202612'  -- justera till relevant intervall
),
base_pick AS (
  -- Välj högsta prio-bas per (bolag, period). MAN/IMP_ADJ är INTE alternativ till basen.
  SELECT company_id, period,
    CASE
      WHEN bool_or(source_kind='SIE_PSALDO') THEN 'SIE_PSALDO'
      WHEN bool_or(source_kind='SIE_VER') THEN 'SIE_VER'
      WHEN bool_or(source_kind='SIE') THEN 'SIE'
      WHEN bool_or(source_kind='SAFT') THEN 'SAFT'
      WHEN bool_or(source_kind='IMP') THEN 'IMP'
    END AS base_src
  FROM fb_signed
  WHERE source_kind IN ('SIE_PSALDO','SIE_VER','SIE','SAFT','IMP')
  GROUP BY company_id, period
),
targets AS (
  SELECT * FROM (VALUES ('202504'),('202604'),('202512')) AS t(target_period)
),
base_ytd AS (
  -- KRITISKT: olika period-logik för olika source_kinds
  SELECT t.target_period, bp.company_id, fb.account_code, SUM(fb.amount) AS amount
  FROM targets t
  JOIN base_pick bp ON bp.period = t.target_period
  JOIN fb_signed fb ON fb.company_id = bp.company_id AND fb.source_kind = bp.base_src
  WHERE 
    -- YTD-källor: ta exakt target_period
    (bp.base_src IN ('SIE','SIE_VER','SAFT') AND fb.period = t.target_period)
    OR
    -- Monthly-källor: summera jan..target
    (bp.base_src IN ('SIE_PSALDO','IMP') AND fb.period BETWEEN substring(t.target_period,1,4) || '01' AND t.target_period)
  GROUP BY t.target_period, bp.company_id, fb.account_code
),
adj_ytd AS (
  -- MAN och IMP_ADJ läggs ALLTID ovanpå basen, oavsett vilken bas. Alltid monthly → summera.
  SELECT t.target_period, fb.company_id, fb.account_code, SUM(fb.amount) AS amount
  FROM targets t
  JOIN fb_signed fb ON fb.source_kind IN ('MAN','IMP_ADJ')
    AND fb.period BETWEEN substring(t.target_period,1,4) || '01' AND t.target_period
  GROUP BY t.target_period, fb.company_id, fb.account_code
),
ytd_combined AS (
  SELECT target_period, company_id, account_code, SUM(amount) AS amount FROM (
    SELECT * FROM base_ytd UNION ALL SELECT * FROM adj_ytd
  ) u GROUP BY target_period, company_id, account_code
)
SELECT json_agg(row_to_json(t))::text AS payload FROM (
  SELECT y.target_period, c.company_id, c.name, c.country, c.currency, c.kind, c.parent_id,
    ag.top_group, ROUND(SUM(y.amount)::numeric, 0)::float AS amount_local
  FROM ytd_combined y
  JOIN acc_topgroup ag ON ag.company_id = y.company_id AND ag.account_code = y.account_code
  JOIN dim_company c ON c.company_id = y.company_id
  GROUP BY y.target_period, c.company_id, c.name, c.country, c.currency, c.kind, c.parent_id, ag.top_group
) t;
```

## Personal-data — använd reporting-vyn

`public.fact_personnel` är PII-spärrad för `mcp_readonly`. Använd `reporting.personnel`:

```sql
WITH snapshots AS (
  SELECT * FROM (VALUES
    (DATE '2025-04-30', 'apr_2025'),
    (DATE '2025-12-31', 'dec_2025'),
    (DATE '2026-04-30', 'apr_2026')
  ) AS s(snap_date, snap_label)
),
fte_at AS (
  SELECT s.snap_label, p.company_id,
    -- COALESCE viktigt: vissa SE-rader saknar pct men ska räknas som heltid
    SUM(COALESCE(p.employment_pct, 1.0)) AS fte,
    COUNT(*) AS headcount
  FROM reporting.personnel p
  CROSS JOIN snapshots s
  WHERE p.employed_from <= s.snap_date
    AND (p.employed_to IS NULL OR p.employed_to > s.snap_date)
  GROUP BY s.snap_label, p.company_id
),
-- Hires/leavers separat per period
...
```

Glöm inte att räkna brutto-rörelse (hires + leavers separat), inte bara netto.

## NO YTD-syntes från journal_saft — ANVÄND INTE (borttaget i v1.4)

⚠️ **Detta mönster är pensionerat.** `fact_journal_saft` är bara ~6% inläst för 2025 → syntes ger fabricerade siffror (~1% av facit). Se pitfall #11.

Bolag som saknar månadsvis SAFT 2025 (de 36 i `build_ru_aggregat.FULL_YEAR_ONLY_2025`) hanteras i stället med **helårsproxy**: använd bolagets egna `SAFT`-saldon för `period='202512'` (finns redan i YTD_TOPGROUP_QUERY:s 202512-target) och jämför mot Mercurs HELÅRSSIFFRA, inte YTD apr. De flaggas `FULL_YEAR_PROXY_2025` och deras financial-YoY mot 202504 nullas. Se pitfall #12.

## KRITISKT: Shared P-koder (P_30, P_35, P_46, P_70 m.fl.)

P-koder är Mercurs P-koder för aggregerade kostnader (P_30 = Försäljning, P_35 = Övrig Försäljning, P_46 = Materialkost, P_70 = Personalkost m.fl.). De finns i `dim_account_map` som `is_aggregated=FALSE` MEN `company_id=NULL` (delas mellan alla bolag).

**Min normala walk-CTE filtrerar på `company_id IS NOT NULL`** och missar därmed P-koder. När MAN-bokningar görs direkt på P-koder (vilket är vanligt) hamnar de utanför top_group-aggregeringen.

**Lösning: dubbel walk + COALESCE-join:**

```sql
WITH RECURSIVE 
  walk_company AS (...med company_id IS NOT NULL...),  -- per-bolag rollup
  walk_shared AS (
    -- Shared rollup för P-koder och liknande
    SELECT m.account_id AS account_code, m.account_id AS cur_id, m.parent_id, 0 AS depth
    FROM dim_account_map m
    WHERE m.is_aggregated = FALSE AND m.company_id IS NULL
    UNION ALL
    SELECT w.account_code, p.account_id, p.parent_id, w.depth + 1
    FROM walk_shared w JOIN dim_account_map p ON w.parent_id = p.account_id WHERE w.depth < 10
  ),
  acc_topgroup_company AS (
    SELECT DISTINCT ON (company_id, account_code) company_id, account_code, cur_id AS top_group
    FROM walk_company WHERE cur_id IN ('Total Sales',...) ORDER BY company_id, account_code, depth DESC
  ),
  acc_topgroup_shared AS (
    SELECT DISTINCT ON (account_code) account_code, cur_id AS top_group
    FROM walk_shared WHERE cur_id IN ('Total Sales',...) ORDER BY account_code, depth DESC
  ),
  ...
  ytd_with_topgroup AS (
    -- COALESCE: prioritera per-bolag, fall back till shared
    SELECT y.target_period, y.company_id, y.account_code, y.amount,
           COALESCE(agc.top_group, ags.top_group) AS top_group
    FROM ytd_combined y
    LEFT JOIN acc_topgroup_company agc ON agc.company_id=y.company_id AND agc.account_code=y.account_code
    LEFT JOIN acc_topgroup_shared ags ON ags.account_code=y.account_code
  )
```

**Effekt av denna fix (verifierat):**
- Lövestad cid 13 Total Sales: 14.48 MSEK → 16.56 MSEK (matchar facit 16.6)
- Koncerntotal 2025: −0.2% diff → 0.00% diff
- Antal röda RUs för 2026: 8 → 3
- Mappning fungerar oberoende av om P-koderna gäller Total Sales (P_3x), Direct Cost (P_4x), Personnel (P_7x/P_8x) etc.

**Shared P-koder och deras top_group-mappning (26 stycken):**
- Total Sales: P_30, P_31, P_32, P_33, P_35
- Total Direct Cost: P_40, P_41, P_42, P_43, P_46, P_49
- Premises: P_50
- Transportation: P_56
- Consultants: P_59
- Other External Costs: P_60, P_61, P_68, _ (underscore), BUDG
- Personnel: P_70, P_80, P_81, P_82, P_83, P_84, P_89

---

## Aaro-rollup för facit-jämförelse

För att jämföra mot Mercur Resultaträkning (21).xlsx på aaro-nivå:

```sql
WITH RECURSIVE walk AS (
  SELECT m.company_id, m.account_code, m.account_id AS cur_id, m.parent_id, m.description, m.is_aggregated, 0 AS depth
  FROM dim_account_map m WHERE m.is_aggregated = FALSE AND m.company_id IS NOT NULL
  UNION ALL
  SELECT w.company_id, w.account_code, p.account_id, p.parent_id, p.description, p.is_aggregated, w.depth + 1
  FROM walk w JOIN dim_account_map p ON w.parent_id = p.account_id WHERE w.depth < 10
),
acc_to_aaro AS (
  -- Hitta CLOSEST aaro-konto (närmaste parent som är is_aggregated och har 4-siffrig prefix)
  SELECT DISTINCT ON (company_id, account_code) 
    company_id, account_code, cur_id AS