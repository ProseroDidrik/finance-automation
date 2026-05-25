
# finance-warehouse query guide

Postgres-warehouse (Azure DB for PostgreSQL Flexible Server) för Prosero-
koncernens nordiska bolag (SE/NO/FI/DK/DE + CENTR/CA). Star schema kring
`fact_balances`. Det här dokumentet fångar **semantiken som inte syns i DDL** —
läs `describe_schema` för aktuella tabeller, använd den här guiden för *hur*
man läser dem utan att räkna fel.

## TL;DR — workflow per fråga

1. **`describe_schema` först** i nya sessioner — bekräftar tabellnamn + live radantal.
2. Identifiera mönster: vilken tabell, vilket `period_type`, vilken source_kind-prioritet?
3. Skriv queryn med `scenario='A'` (utfall) som default och rätt best_source per land.
4. Vid valuta-jämförelse: konvertera via `dim_exchange_rate` med `rate_type='avg'`.
5. Vid tveksamhet: visa SQL för användaren innan stora aggregeringar körs.

## Postgres-syntax (cloud sedan 2026-05-11)

- `STRING_AGG`, inte `LIST_AGG`
- Inget `QUALIFY` — wrappa i CTE/subquery
- `to_char(date_col, 'YYYYMM')` istället för DuckDB:s `strftime(date, '%Y%m')`
- `DATE_TRUNC('month', ts::date)` istället för DuckDB-date-funktioner
- Inga `USING SAMPLE`, ingen `FILTER`-syntax på `COUNT` (Postgres stödjer den
  faktiskt, men håll det enkelt med `SUM(CASE WHEN ... THEN 1 ELSE 0 END)`)
- I parametriserade queries: escape `%` som `%%` (psycopg-parameter-rendering)

## Tabeller — quick map

| Tabell | Innehåll |
|---|---|
| `dim_company` | bolagsregister + förvärvsdata (`closing_date`, `ev_sek_m`, `ebitda_ltm`, `sales_ltm`, `investment_currency`) |
| `dim_period` | YYYYMM → år/månad/kvartal/datum |
| `dim_account_map` | kontoplan + P&L-trädet (rekursivt via `parent_id`, rot = `'P&L'`) |
| `dim_exchange_rate` | (period, currency, rate_type='avg'\|'constant') → SEK per enhet utländsk |
| `fact_balances` | **huvudfakta**: saldon per (bolag, period, konto, källa, scenario) |
| `fact_journal_sie` | transaktionsrader (Sverige) — opt-in via load_sie.py. ⚠️ MCP: använd `reporting.journal_sie` |
| `fact_journal_saft` | transaktionsrader (Norge) — opt-in via load_saft.py. ⚠️ MCP: använd `reporting.journal_saft` |
| `fact_personnel` | snapshot per anställd. ⚠️ MCP: använd `reporting.personnel` |
| `dim_supplier_register` | `(country, levprefix)` → `supplier_name`, `kategori`, `segment` |
| `fact_supplier_spend` | spend per (bolag, leverantör, år, period_kind) — endast helår/H1 |
| `backup_from_mercur` | Mercur-facit för datatäckning (se § Facit nedan) |
| `load_history` | log över alla körningar |
| `reporting.personnel` | **PII-minimerad personalvy** — pseudonym `EMP_{id}`, `birth_year` istället för datum |
| `reporting.journal_sie` | **SIE-verifikat med PNR maskat** till `[PNR]` i `voucher_text` + `transaction_text` |
| `reporting.journal_saft` | **SAF-T-rader med PNR maskat** till `[PNR]` i `line_description` + `transaction_description` |

### Mental model 0 — PII-läsning via reporting-vyer (T3 2026-05-25)

MCP-rollen `mcp_readonly` har **ingen direktaccess** på `public.fact_personnel`,
`public.fact_journal_sie` eller `public.fact_journal_saft`. Försök ger
`ERROR: permission denied`. Använd alltid:

```sql
-- ❌ ger permission denied:
SELECT * FROM fact_personnel WHERE company_id = 134;

-- ✅ funkar, med pseudonym + grovkornad födelseinfo:
SELECT employee_ref, birth_year, employment_pct, location
FROM reporting.personnel
WHERE company_id = 134;
```

Vyerna är 1:1-mappning av rådata med tre justeringar:
- `employee_name` → `employee_ref` (pseudonym `EMP_{id}`)
- `birth_date` → `birth_year`
- Svenska personnummer i fritext (regex `[0-9]{6}[-+][0-9]{4}`) → `[PNR]`
- `salary_local`, `termination_reason` borttagna (AWAITING_DPO)

Behöver du verkligen rådata: lägg en uppgift om utökad åtkomst — den ska gå
via en separat roll, inte via mcp_readonly.

---

## Mental model 1 — `period_type` ('ytd' vs 'monthly')

`fact_balances.amount` betyder **olika saker per source_kind**:

| `source_kind` | Land | `period_type` |
|---|---|---|
| `SIE`, `SIE_PSALDO` | Sverige + CA | `'ytd'` (ackumulerat från 1 jan) |
| `SAFT` | Norge | `'ytd'` |
| `IMP` | FI/DK/DE/CENTR + Mercur-historik alla | `'monthly'` (rörelse just den månaden) |
| `IMP_ADJ`, `MAN` | Mercur-justeringar, alla länder | `'monthly'` |
| `IB` | (ingående balans 202112) | `'monthly'` |

**Aldrig `SUM(amount)` rakt över länder utan att normalisera.** Mönster:

- **Månadsbelopp:** `cur.amount - prev_period.amount` om `period_type='ytd'`, annars `cur.amount`.
- **YTD-belopp:** `cur.amount` om `period_type='ytd'`, annars `SUM(amount)` jan..valt period.
- **Färdigt mönster** i `webapp/backend/sql/report_pnl.sql:112-143` (`balances` CTE) — kopiera direkt.

YTD-konverteringen antar **kalenderår**. Bolag med brutet räkenskapsår skulle ge fel värden (inga finns idag, flagga om någon laddas).

---

## Mental model 2 — `best_source` (bas-källa) + additiva justeringslager

Verkligt utfall = **en bas-källa** + **additiva justeringslager**. Två skilda
saker — blanda inte ihop dem.

**Bas-källan** — välj *en*. `SIE_PSALDO`/`SIE_VER`/`SIE` (och `SAFT`/`IMP`) är
mutuellt uteslutande representationer av samma huvudbok; summera dem aldrig.

| Land | Bas-prioritet (scenario='A') |
|---|---|
| Sweden | `SIE_PSALDO` → `SIE_VER` → `SIE` → `IMP` |
| Norway | `SAFT` → `IMP` |
| Finland, Denmark, Germany, CENTR | `IMP` |
| CA | `SIE` → `IMP` |

**Justeringslagren** `MAN` och `IMP_ADJ` (scenario='A') är *aldrig* alternativ
till basen — de **summeras alltid ovanpå**. Saknas bas-källa (t.ex. bara
`IMP_ADJ` finns) är justeringslagret hela utfallet:

    utfall = bas-källa (0 eller 1)  +  MAN-A  +  IMP_ADJ-A

`SIE_PSALDO` = `#PSALDO`-raderna i SIE-filen (källrapporterat per-månads YTD-saldo) — bäst när det finns. `SIE_VER` = YTD-saldon syntetiserade av `load_sie.py` från verifikaten (`#VER`/`#TRANS`) för de ~35 SE-bolag som saknar `#PSALDO`; ger exakt månadsfördelning. `SIE` (#RES-baserad) är därmed effektivt deprekerad — kvar bara som sista fallback om verifikat-syntesen inte kunnat köras. När både finns: `SIE_PSALDO` > `SIE_VER` > `SIE`.

Färdigt mönster: `report_pnl.sql` — `best_source`-CTE väljer basen, och
`raw_balances` OR-villkoret summerar `MAN`/`IMP_ADJ` ovanpå.

**OBS — INL är borta:** sedan 2026-05-cutovern lagrar `load_inl.py` (FI/DK/DE)
data med `source_kind='IMP'`, inte `'INL'`. Äldre dokumentation kan referera
till `INL` — det är samma sak som `IMP` i koden idag.

---

## Mental model 3 — Tecken­konvention

Rådata följer **SIE-konvention**: intäkt **negativ**, kostnad **positiv**,
tillgång positiv, skuld/eget kapital negativ.

**Undantag 1 — `P_*`-konton (Mercur P-koder)** är lagrade i **Mercur-konvention**
(intäkt positiv). `report_pnl.sql:98` flippar tecknet:

```sql
SUM(fb.amount * CASE WHEN fb.account_code LIKE 'P_%' THEN -1 ELSE 1 END)
```

Frontend gör ytterligare display-flip via `pnl_kpis.yaml`. **När du frågar
`fact_balances` direkt får du SIE-rådatat**, inte presentationen.

**Undantag 2 — `backup_from_mercur` för SIE/SAFT** är lagrat i **Mercur-konvention**
(intäkt +, kostnad −), medan `fact_balances` för samma källor följer SIE-
konvention (intäkt −, kostnad +). Mercur re-exporterar SIE/SAFT-data med
sign-flip när de bygger sin backup-fil. För `IMP`/`MAN`/`IMP_ADJ` följer båda
sidor Mercur-konvention och ingen flip behövs.

| `source_kind` | `backup_from_mercur` | `fact_balances` | Flip vid jämförelse |
|---|---|---|---|
| `SIE` / `SAFT` | Mercur (intäkt +) | SIE (intäkt −) | **Ja** — `SUM(-backup.amount)` |
| `IMP` / `MAN` / `IMP_ADJ` | Mercur | Mercur | Nej |

Empiriskt verifierat 2026-05-18: för SE-SIE (54 075 konto-perioder) och NO-SAFT
(2 897) ligger 96 % resp 90 % närmare sign-flippad än naiv jämförelse. Mönster:
`webapp/backend/sql/compare_coverage.sql:97` och `coverage_accounts.sql:75`.

---

## Mental model 4 — Scenario-filter

`fact_balances.scenario`:
- `'A'` = utfall (Actuals) — default för rapporter
- `'B'` = budget

**Nyans för `MAN`:** Den primära användningen är budget (`scenario='B'`), men
det finns **också MAN-rader för utfall** (`scenario='A'`) — manuella
justeringar på utfallssiffrorna. Likaså `IMP_ADJ` (finns bara som scenario A).

`report_pnl.sql` **summerar `MAN`-A + `IMP_ADJ`-A ovanpå bas-källan** (se
Mental model 2) — verkligt utfall = bas + justeringslager. Budget-kolumnen kör
samma query med `scenario='B'` och source-override `'MAN'`.

**Default i alla utfallsfrågor:** `WHERE scenario = 'A'`. Glömmer du det dubbleras
utfall + budget.

---

## Tabellspecifika anteckningar

### `fact_personnel` är snapshot, inte tidsserie

En rad **per anställd**, inte per (anställd, period):

- `employed_from`, `employed_to` (NULL = aktiv) — räkna ut aktivitet vid valt datum själv.
- `employment_pct` (1.0 = heltid) — **summera detta för FTE**, inte `COUNT(*)` (det blir headcount).
- Endast SE/NO/FI har data — DK/DE saknas.
- `salary_local` är ifyllt bara för FI (i `dim_company.currency`).

```sql
-- Aktiv FTE per ett datum:
SELECT company_id, SUM(employment_pct) AS fte
FROM fact_personnel
WHERE employed_from <= DATE '2026-04-30'
  AND (employed_to IS NULL OR employed_to > DATE '2026-04-30')
GROUP BY company_id;
```

### `dim_company` förvärvsdata (kol K–P i Dotterbolagslistan)

Sex fält som speglar bolaget **vid köptillfället** — `NULL` för CENTR/CA och
organiska bolagsbildningar (~112/147 bolag har värde):

| Kolumn | Notering |
|---|---|
| `closing_date` | Datum för köp (DATE) |
| `investment_currency` | Valuta för LTM-siffrorna. I praktiken = `currency` idag |
| `ev_sek_m` | **OBS — namnet vilseleder**. Header säger "EV (SEKm)" men värdena är råa belopp i `investment_currency`, inte miljoner SEK |
| `ev_ebitda_ltm` | **Skalningsbug** i Excel: värdet är multipeln × 10⁶ (5,7M = 5.7x). Dela med `1e6`, eller räkna själv som `ev_sek_m / ebitda_ltm` |
| `ebitda_ltm`, `sales_ltm` | Råa belopp i `investment_currency` |

**Filtrera alltid `WHERE ev_sek_m IS NOT NULL`** vid förvärvsanalyser.

```sql
-- EV-konvertering till SEK på köpdagen (Postgres: to_char, inte strftime):
SELECT c.company_id, c.name, c.country, c.closing_date,
       c.ev_sek_m * COALESCE(r.rate, 1.0) AS ev_sek
FROM dim_company c
LEFT JOIN dim_exchange_rate r
  ON r.currency = c.investment_currency
 AND r.rate_type = 'avg'
 AND r.period = to_char(c.closing_date, 'YYYYMM')
WHERE c.ev_sek_m IS NOT NULL
ORDER BY ev_sek DESC;
```

### `fact_supplier_spend` — inte månadsvis

- `period_kind`: `'FULL'` (helår) eller `'H1'`. Inga andra värden, ingen månadsupplösning.
- `year`: 2021..2025. **Ingen 2026-data än.**
- `amount` i `currency` (bolagets lokala) — konvertera via `dim_exchange_rate` för cross-country jämförelse.
- `levprefix` är joinnyckel mot `dim_supplier_register` på `(country, levprefix)`.
- `company_id` kan vara `NULL` — använd `bolag_label` som fallback.
- `kategori`/`segment` finns på både dim och fact (snapshot vid laddning) — föredra `dim_supplier_register` för "senaste klassificering".
- **Bara Sverige har data** (`country='Sweden'`) idag.

```sql
-- Topp 20 leverantörer 2024:
SELECT COALESCE(d.supplier_name, f.namn) AS supplier,
       d.kategori, d.segment, SUM(f.amount) AS spend
FROM fact_supplier_spend f
LEFT JOIN dim_supplier_register d
  ON d.country = f.country AND d.levprefix = f.levprefix
WHERE f.country = 'Sweden' AND f.year = 2024 AND f.period_kind = 'FULL'
GROUP BY 1, 2, 3
ORDER BY spend DESC
LIMIT 20;
```

### `dim_account_map` — rekursivt P&L-träd

Hierarki med rot `account_id = 'P&L'` och `parent_id`-pekare uppåt.
`(company_id, account_code)` är join-nyckeln mot `fact_balances`.

Gruppkonton (`is_aggregated = TRUE`) har inga directa balances — de aggregerar
via barnen. Vanliga rotnoder utöver `P&L`: `B` (Balans), `BUD` (Budget),
`FTE`, `ÅRRES`.

Hela trädet: `report_pnl.sql:24-35` (`accounts` CTE med `WITH RECURSIVE`).

**AARO-konton.** Gruppkonton (`is_aggregated = TRUE`) vars `description` börjar
med ett 4-siffrigt prefix — t.ex. `'4010 Cost of goods sold, external, COGS'`
eller `'7610 Other personnel costs'` — kallas i dagligt tal *aaro-konton*. Det
4-siffriga numret är koncernens standardiserade AARO-kontonummer; ~258 rader
har en sådan beskrivning. Det är en mycket användbar grupperingsnivå: varje
bolagskonto (t.ex. `170_4110`) pekar via `parent_id` upp på sitt aaro-konto,
och aaro-kontots eget `parent_id` är ofta ett Mercur-namn (`Materialkost`,
`Personalkostnader`) som återfinns i Mercur.

```sql
-- Alla aaro-konton:
SELECT account_id, description, parent_id
FROM dim_account_map
WHERE is_aggregated AND description ~ '^[0-9]{4}( |$)'
ORDER BY description;

-- Rulla upp ett bolagskonto till dess aaro-konto:
WITH RECURSIVE walk AS (
  SELECT account_id, description, parent_id, is_aggregated
  FROM dim_account_map WHERE account_id = '170_4110'
  UNION ALL
  SELECT m.account_id, m.description, m.parent_id, m.is_aggregated
  FROM dim_account_map m JOIN walk w ON m.account_id = w.parent_id
)
SELECT * FROM walk
WHERE is_aggregated AND description ~ '^[0-9]{4}( |$)'
LIMIT 1;
```

---

## Facit: `backup_from_mercur`

Mercur-export av samma utfall som ska hamna i `fact_balances`. Används som
**sanity-check** för att verifiera att vår ETL fångat allt Mercur har.

**Källor i tabellen** (efter 2026-05-13-laddningen av `2026 Backup.txt`):

- `IMP` — FI/DK/DE-utfall (matchar fact.IMP exakt, både rader och summa)
- `SIE` — SE-utfall (matchar fact.SIE/SIE_PSALDO efter YTD-cum + sign-flip)
- `SAFT` — NO-utfall (samma behandling som SIE)
- `MAN`, `IMP_ADJ` — manuella justeringar

**KRITISKT — två normaliseringar behövs för SIE/SAFT** innan amount-jämförelse:

1. **Period:** backup lagrar *månadsbevegelser* (M-rader), fact lagrar *YTD-saldon*.
   YTD-cum:a backup jan..period innan jämförelse.
2. **Tecken:** backup är i Mercur-konvention, fact i SIE-konvention. Sign-flippa
   backup (`SUM(-amount)` i YTD-cumen). Se Mental model 3, Undantag 2.

| Källa | backup.amount | fact.amount | Jämförbarhet |
|---|---|---|---|
| `IMP`, `MAN`, `IMP_ADJ` | monthly, Mercur-sign | monthly, Mercur-sign | ✅ direkt |
| `SIE`, `SAFT` | monthly, Mercur-sign | YTD, SIE-sign | ✅ efter YTD-cum + `SUM(-amount)` |

För BS-konton i SIE/SAFT klassas jämförelsen som `no_baseline` — YTD-cum kräver
korrekt IB (ingående balans) som inte alltid finns. Endast IS-konton kan
värde-jämföras tillförlitligt.

### Förväntat brus i SIE-jämförelse: `#PSALDO`-frånvaron

> **Uppdaterat 2026-05-20:** `load_sie.py` syntetiserar numera `SIE_VER` (YTD
> kumulerat från verifikaten) för bolag utan `#PSALDO`. `report_pnl.sql` och
> `report_pivot.sql` väljer `SIE_VER` före `SIE`, så #RES-timing-bruset nedan
> gäller inte längre P&L-rapporterna. `compare_coverage.sql` påverkas inte —
> den läste redan `fact_journal_sie` direkt.

35 av 49 svenska SIE-bolag (~71 % per 2026-05) saknar `#PSALDO`-rader i sin
SIE-export. Konsekvensen är att `load_sie.py` använder `#RES`-fältet (årets
totala resultat) som YTD-källa, och taggar värdet med `--period`-parametern.
`#RES` är en snapshot **vid SIE-genereringstiden**, inte exakt YTD per
månadsskifte.

Praktisk konsekvens: när bolaget genererar SIE 11 maj för "april-data" hamnar
typiskt 11 dagar maj-bokningar i `#RES`. Vi taggar värdet som `period=202604`
men beloppet är YTD per ~11 maj. Mot Mercur-backupens jan-april summa ger
detta amount_diff av storleksordning 5–30 % per konto — ren timing-brus,
inte verklig ETL-bug.

**Bolag som påverkas** (saknar `SIE_PSALDO`-rader i fact_balances):

```sql
WITH sie_b AS (SELECT DISTINCT company_id FROM fact_balances
               WHERE source_kind = 'SIE' AND scenario = 'A'),
     psaldo_b AS (SELECT DISTINCT company_id FROM fact_balances
                  WHERE source_kind = 'SIE_PSALDO' AND scenario = 'A')
SELECT c.company_id, c.name FROM dim_company c
WHERE c.company_id IN (SELECT company_id FROM sie_b)
  AND c.company_id NOT IN (SELECT company_id FROM psaldo_b);
```

För dessa bolag är amount_diff i täckningsmatrisen *förväntat brus*, inte
fel. Bolag med `#PSALDO` (14 av 49 SE) har exakt YTD per månadsskifte och
ska matcha Mercur-backupen inom 1 %-tröskeln.

**Egentliga ETL-buggar separeras genom storleken på diff:** timing-brus är
typiskt 1–30 % per konto med konsistent riktning (fact > facit eftersom
extra dagar adderar bokningar). Större diff (50 %+) eller systematisk
~5× ratio är troligen **kontoplansmappning** — bolagets SIE-kontoplan
matchar inte Mercurs konsoliderade BAS-mappning. Bolag 164 (El &
Fastighetsdrift) är ett känt exempel.

### Förväntat brus i SAFT-jämförelse: Tripletex `ClosingBalance` vs GL

`load_saft.py` läser kontosaldon ur `Account/ClosingCreditBalance` (det
auktoritativa saldot i SAF-T-XML:n) — inte ur summan av GL-entries.
Mercurs egen NO-parser räknar i stället månadsrörelse från
`GeneralLedgerEntries`. För 33 av 35 NO-bolag är de identiska och allt
matchar exakt. För **2 av 7 Tripletex-bolag** (158 Asker, 189 Lås &
Prosjekt, per 2026-04) ligger `ClosingBalance` några hundra tusen NOK
högre än cumsum av GL-entries i samma fil — alltså transaktioner som
påverkar saldot men inte exporteras som rena GL-rader (troligen
periodlåsta justeringar eller aviavtryck i Tripletex). Detta är *inte*
en ETL-bug, och inte filspecifikt — diffen återkommer per fil från
samma två bolag oavsett när SAFT genereras.

Empiriska siffror (2026-04, konto 3000):

| Bolag | ClosingBalance (fact) | GL cumsum (Mercur) | Diff |
|---|---:|---:|---:|
| 158 Asker | 3 203 730 | 3 094 350 | +109 380 (+3,4 %) |
| 189 Lås & Prosjekt | 8 144 680 | 7 881 520 | +263 160 (+3,2 %) |

Övriga 5 TT-bolag (36, 91, 111, 148, 237) har 0 diff. PowerOffice-bolagen
19 och 171 har försumbar diff (<0,1 %) som sannolikt är öresavrundning.

**Praktisk konsekvens:** rapporter mot `fact_balances` ger det
auktoritativa balanssaldot (vad bokföringssystemet rapporterar);
`compare_all_file_vs_db` mot Mercur kommer visa ~3 % avvikelse för dessa
två bolag och det är förväntat — inte indikation på att SAFT-fil eller
ETL behöver åtgärdas. För att förändra det måste antingen bolaget
ändra sin Tripletex-exportkonfiguration eller Mercur byta till
ClosingBalance-baserad inläsning.

Identifiera per session med:
```sql
WITH gl AS (
  SELECT company_id, SUM(-amount) AS gl_sum
  FROM fact_journal_saft
  WHERE account_code = '3000'
    AND transaction_date BETWEEN DATE '2026-01-01' AND DATE '2026-04-30'
  GROUP BY 1)
SELECT fb.company_id, c.name, SUM(-fb.amount) AS fact, gl.gl_sum,
       SUM(-fb.amount) - gl.gl_sum AS diff
FROM fact_balances fb
JOIN dim_company c USING (company_id)
LEFT JOIN gl USING (company_id)
WHERE fb.source_kind = 'SAFT' AND fb.scenario = 'A'
  AND fb.period = '202604' AND fb.account_code = '3000'
GROUP BY 1, 2, gl.gl_sum
HAVING ABS(SUM(-fb.amount) - COALESCE(gl.gl_sum, 0)) > 1000
ORDER BY 5 DESC;
```

### Framtida-period-filter i compare_coverage

`compare_coverage.sql:cutoff`-CTE:n filtrerar bort backup-rader där
`period > föregående kalendermånad`. SIE-filer täcker hela året (PSALDO-
rader för jan-aktuell-månad) och MAN-budgetprognoser sträcker till
202612 — utan filter visas dessa som `missing` i täckningsmatrisen
trots att vi inte är klara med månaden än.

**Mönster för facit-coverage** (`webapp/backend/sql/compare_coverage.sql`):

```sql
-- backup-sidan: aggregera per (bolag, period, källa, scenario)
WITH backup_agg AS (
    SELECT company_id, period, source_kind, scenario,
           COUNT(*) AS rows, SUM(amount) AS total
    FROM backup_from_mercur WHERE scenario = 'A'
    GROUP BY 1,2,3,4
),
-- fact-sidan SE: välj SIE > SIE_PSALDO, taggat som 'SIE' så join funkar
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
           COUNT(*)::int AS rows, SUM(fb.amount) AS total
    FROM sie_pick p
    JOIN fact_balances fb USING (company_id, period, scenario)
    WHERE fb.source_kind = p.picked_kind
    GROUP BY fb.company_id, fb.period, fb.scenario
),
-- IMP/SAFT/MAN/IMP_ADJ rakt av
fact_other AS (
    SELECT company_id, period, source_kind, scenario,
           COUNT(*)::int AS rows, SUM(amount) AS total
    FROM fact_balances
    WHERE source_kind IN ('IMP', 'SAFT', 'MAN', 'IMP_ADJ') AND scenario = 'A'
    GROUP BY 1,2,3,4
)
SELECT b.*, f.rows AS fact_rows, f.total AS fact_sum,
       CASE
           WHEN f.company_id IS NULL THEN 'missing'  -- finns i facit, inte i fact
           WHEN b.source_kind IN ('IMP','MAN','IMP_ADJ')  -- monthly: jämför summor
                AND ABS(b.total - f.total) > 1
                AND ABS(b.total - f.total) > 0.01 * NULLIF(ABS(b.total), 0)
           THEN 'mismatch'
           ELSE 'ok'
       END AS status
FROM backup_agg b
LEFT JOIN (SELECT * FROM fact_sie UNION ALL SELECT * FROM fact_other) f
  USING (company_id, period, source_kind, scenario);
```

**Tröskel-trick:** lägg till absolut tolerans (`> 1` enhet) utöver relativa
1 %-villkoret, annars trillar floating-point-brus runt 0 in som mismatch.

---

## Övriga frågemallar

### Ad hoc P&L för (bolag, period)

Använd helst `report_pnl.sql` via webapp-endpointen `/api/report/pnl`. För
direkt query mot warehouse (OBS: mönstret nedan visar bara **bas-källan** —
för verkligt utfall summera `MAN`-A + `IMP_ADJ`-A additivt ovanpå, se Mental
model 2):

```sql
WITH best AS (
  SELECT company_id, period,
    CASE
      WHEN MAX(CASE WHEN source_kind='SIE'        THEN 1 ELSE 0 END)=1 THEN 'SIE'
      WHEN MAX(CASE WHEN source_kind='SIE_PSALDO' THEN 1 ELSE 0 END)=1 THEN 'SIE_PSALDO'
      WHEN MAX(CASE WHEN source_kind='SAFT'       THEN 1 ELSE 0 END)=1 THEN 'SAFT'
      WHEN MAX(CASE WHEN source_kind='IMP'        THEN 1 ELSE 0 END)=1 THEN 'IMP'
      WHEN MAX(CASE WHEN source_kind='IMP_ADJ'    THEN 1 ELSE 0 END)=1 THEN 'IMP_ADJ'
    END AS source_kind
  FROM fact_balances
  WHERE company_id = ? AND period = ? AND scenario = 'A'
  GROUP BY company_id, period
)
SELECT fb.account_code, fb.account_name,
       fb.amount * CASE WHEN fb.account_code LIKE 'P_%' THEN -1 ELSE 1 END AS amount
FROM fact_balances fb
JOIN best USING (company_id, period, source_kind)
WHERE fb.scenario = 'A'
ORDER BY fb.account_code;
```

För SE/NO: amount = YTD direkt. För FI/DK/DE: amount = bara den månadens
bevegelse — för YTD jan..period måste du `SUM` över alla månader.

### Valuta till SEK

```sql
SELECT fb.*, fb.amount * COALESCE(r.rate, 1.0) AS amount_sek
FROM fact_balances fb
JOIN dim_company c USING (company_id)
LEFT JOIN dim_exchange_rate r
  ON r.period = fb.period AND r.currency = c.currency AND r.rate_type = 'avg'
WHERE c.currency != 'SEK';
```

### Vilka bolag har data för en period?

```sql
SELECT period, c.country, COUNT(DISTINCT fb.company_id) AS bolag
FROM fact_balances fb
JOIN dim_company c USING (company_id)
WHERE fb.scenario = 'A' AND fb.source_kind IN ('SIE','SIE_PSALDO','SAFT','IMP')
GROUP BY 1, 2 ORDER BY 1, 2;
```

---

## Anti-mönster — gör inte detta

❌ `SELECT SUM(amount) FROM fact_balances WHERE period = ?`
   — blandar YTD/monthly + dubblar källor.

❌ Jämför `amount` mellan SE och DK utan period_type-normalisering.

❌ Använd `fact_balances.amount` direkt som "intäkt" utan teckenflip för
   `P_*`-koder eller utan att veta att SIE-tecken är default.

❌ `COUNT(*) FROM fact_personnel` ≠ FTE. FTE = `SUM(employment_pct)` med
   `employed_to`-filter. `COUNT(*)` är headcount inkl. avslutade.

❌ `SELECT * FROM fact_supplier_spend WHERE period = '202604'` — kolumnen
   heter inte `period`, det finns bara `(year, period_kind='FULL'|'H1')`.

❌ Glöm `WHERE scenario = 'A'` i utfallsfrågor → får med MAN-budget.

❌ Jämför `backup_from_mercur.amount` mot `fact_balances.amount` rakt för
   SE-SIE eller NO-SAFT — två normaliseringar krävs: (1) YTD-kum:a backup
   (monthly → YTD), (2) sign-flippa backup (Mercur → SIE-konvention) via
   `SUM(-amount)`. Mönster: `webapp/backend/sql/compare_coverage.sql:97`.
   För IMP/MAN/IMP_ADJ är båda sidor Mercur-konvention och monthly — ingen
   normalisering behövs.

❌ `strftime(date, '%Y%m')` är DuckDB-syntax. Postgres: `to_char(date, 'YYYYMM')`.

❌ `INL` som source_kind — sedan 2026-05-cutovern är det `IMP` även för
   FI/DK/DE. Äldre dokumentation kan referera till `INL`.

---

## Live snapshot — radantal per tabell

Kör `describe_schema` i början av varje session för aktuella siffror. Som
referens (2026-05-13):

| Tabell | Rader |
|---|---:|
| `dim_company` | 147 |
| `dim_account_map` | ~80 700 |
| `fact_balances` | ~430 000 |
| `backup_from_mercur` | ~430 000 (efter 2026-facit-laddning) |
| `fact_journal_sie` | ~820 000 |
| `fact_journal_saft` | ~910 000 |
| `fact_personnel` | ~3 070 |
| `fact_supplier_spend` | ~47 700 |

Bolag per land: SE 61 · NO 42 · FI 21 · CENTR 8 · DK 8 · DE 5 · CA 2.
