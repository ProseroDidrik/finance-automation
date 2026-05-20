# Coverage Quality: per-konto-mismatch för SIE/SAFT + drilldown-drawer

**Datum:** 2026-05-17
**Scope:** webapp (täckningssidan) — SQL-omskrivning av mismatch-klassificering + ny drilldown-endpoint + UI-drawer.

## 1. Översikt & mål

Två sammanhängande förbättringar av täckningssidan, delad infrastruktur:

- **#1 SIE/SAFT-mismatch:** byt `mismatch`-klassificering till per-konto-jämförelse för *alla* källor (IMP, SIE, SAFT, MAN, IMP_ADJ). För SIE/SAFT kumuleras facit månadsmovement → YTD innan jämförelse. För övriga jämförs monthly↔monthly direkt (oförändrad semantik utåt, striktare på kontonivå).
- **#2 Drilldown:** klick på bolag-rad i drill-listan på täckningssidan öppnar en högersidopanel med per-konto-diff (`account_code`, `account_name`, `facit`, `fact`, `diff`, `status_acc`). Cold-path, lazy-laddad via egen endpoint.

Mismatch-tröskel per konto: `|diff| > 1.0 AND |diff| > 0.01 * |facit|` — samma 1-enhet-OCH-1%-regel som dagens sum-test, fast per konto. (Justerbar i framtida iteration.)

**Motivering:** `SUM(amount) ≈ 0` per design för SIE/SAFT (trial balance), så sum-baserad mismatch hittar inget. Per-konto-jämförelse är enda meningsfulla nivån — och gör samtidigt IMP-jämförelsen striktare (täcker compensating errors mellan konton som summerar till noll-diff).

## 2. Datamodell & gemensam diff-CTE

Båda queryer (matris-status och drilldown) bygger på en gemensam logisk vy `coverage_accounts_diff` per (`company_id`, `period`, `source_kind`, `scenario`, `account_code`):

```
facit_amt  = för SIE/SAFT: SUM(backup.amount) över period 1..N i samma år
           = för IMP/MAN/IMP_ADJ: backup.amount (monthly, för perioden)
fact_amt   = för SIE: fact_balances där source_kind = picked_kind (SIE eller SIE_PSALDO)
           = för SAFT/IMP/MAN/IMP_ADJ: fact_balances rakt av
diff       = ROUND(facit_amt - fact_amt, 2)
status_acc = 'ok'          om |diff| ≤ max(1.0, 0.01 * |facit_amt|)
             'amount_diff' om båda finns men diff över tröskel
             'only_facit'  om facit_amt finns, fact_amt saknas
             'only_fact'   om fact_amt finns, facit_amt saknas
```

**Gränser:**
- **Scope:** `scenario='A'`, period-scope ärvs från existerande täckningssida (ingen ny WHERE-klausul). Källor SIE/SAFT/IMP/MAN/IMP_ADJ; SIE_PSALDO döljs via picked_kind-fallback (samma logik som idag).
- **YTD-kumulering över årsgräns:** `SUM(...) OVER (PARTITION BY company_id, LEFT(period, 4), source_kind, scenario, account_code ORDER BY period)` — januari startar om från 0. Ingen IB-hantering behövs eftersom backup är komplett för året.
- **Account_name:** `fact.account_name` (kommer alltid från fact-sidan eftersom `backup_from_mercur.account_name` är NULL by design — backup-export från Mercur saknar konto-namn). Vid `only_facit`-rader (saknas i fact) → visa `account_code` ensam.
- **Decimal-precision:** `ROUND(diff::numeric, 2)` innan tröskeltest, så öresfel inte trillar in som amount_diff.

## 3. SQL-ändringar

Två filer i `webapp/backend/sql/`:

### a) `compare_coverage.sql` (uppdaterad)

Behåll dagens struktur (`backup_agg → sie_pick → fact_sie → fact_other → fact_agg → status`), men:

1. Lägg till en `account_diff` CTE som implementerar `coverage_accounts_diff` ovan (FULL JOIN per konto). **CTE-shape:** `UNION ALL` av två sub-queries — en för SIE/SAFT (med YTD-kumulering via window function) och en för IMP/MAN/IMP_ADJ (monthly rakt av). Renare planer än `CASE WHEN ... THEN SUM(...) OVER (...)` i en enda gren, och DuckDB/Postgres-planern kan pruna varje gren oberoende.
2. I `CASE`-uttrycket för `status`: ersätt nuvarande `'mismatch'`-gren (sum-diff för IMP/MAN/IMP_ADJ) med:
   ```sql
   WHEN EXISTS (SELECT 1 FROM account_diff ad
                WHERE ad.company_id  = COALESCE(b.company_id, f.company_id)
                  AND ad.period      = COALESCE(b.period, f.period)
                  AND ad.source_kind = COALESCE(b.source_kind, f.source_kind)
                  AND ad.scenario    = COALESCE(b.scenario, f.scenario)
                  AND ad.status_acc IN ('amount_diff','only_facit','only_fact'))
   THEN 'mismatch'
   ```
3. Uppdatera kommentarsblocket överst: mismatch är nu per-konto för **alla** källor, YTD-kumulering normaliserar SIE/SAFT. Den nuvarande raden om att "mismatch är meningslös för SIE/SAFT" tas bort — det blir inte längre sant.

### b) `coverage_accounts.sql` (ny)

Återanvänder `account_diff`-logiken (börjar med duplicering av CTE:n; refaktorera till delad SQL-fil senare om sliter). Parametrar: `company_id`, `period`, `source_kind` (`scenario` hårdkodas till `'A'`).

Returnerar:
```
account_code, account_name, facit_amt, fact_amt, diff, status_acc
ORDER BY status_acc (amount_diff/only_facit/only_fact först), |diff| DESC, account_code
```

## 4. API

I `webapp/backend/main.py`:

### a) `/api/compare/coverage` (oförändrad signatur)

Får automatiskt ny mismatch-semantik eftersom underliggande SQL bytts. Response-format identiskt. Inga klient-ändringar behövs i UI:t för matrisen.

### b) `/api/compare/coverage/accounts` (ny)

```
GET /api/compare/coverage/accounts
    ?company_id=134
    &period=202604
    &source_kind=IMP
```

**Validering** (samma stil som existerande endpoints):
- `company_id`: int, required
- `period`: `^\d{6}$`, required
- `source_kind`: enum `IMP|SIE|SAFT|MAN|IMP_ADJ`, required (`SIE_PSALDO` accepteras inte; mappas internt via `picked_kind`)

**Response:**
```json
{
  "company_id": 134,
  "company_name": "...",
  "period": "202604",
  "source_kind": "IMP",
  "rows": [
    {
      "account_code": "3010",
      "account_name": "Försäljning ...",
      "facit_amt": -1234567.0,
      "fact_amt": -1234000.0,
      "diff": -567.0,
      "status_acc": "amount_diff"
    }
  ],
  "summary": {
    "n_ok": 42, "n_amount_diff": 3, "n_only_facit": 1, "n_only_fact": 0,
    "facit_sum": -50.0, "fact_sum": 12.5
  }
}
```

**Klient** (`webapp/frontend/src/api.ts`): lägg till `fetchCoverageAccounts({company_id, period, source_kind})` → `CoverageAccountsReport` med interface motsvarande JSON ovan.

## 5. Frontend

I `webapp/frontend/src/components/`:

### a) `CoverageAccountsDrawer.tsx` (ny)

Sidopanel från höger, ~640px bred, overlay-bakgrund. Öppnas/stängs via prop `selection: {company_id, period, source_kind, company_name} | null`.

**Layout:**
- **Header:** bolag, period, källa, stäng-knapp (×; Escape stänger också).
- **Summary-rad:** chips med `n_amount_diff`, `n_only_facit`, `n_only_fact`, `n_ok` (samma färger/ikoner som matrisen). `facit_sum` & `fact_sum`.
- **Tabell:** kolumner `Konto`, `Namn`, `Facit`, `Fact`, `Diff`, `Status`. Sortering klickbar (default: status först, sedan `|diff|` desc — matchar SQL). Default-filter "Visa bara avvikelser" (checkbox-toggle som visar/döljer `ok`-rader).
- **Tom diff-state:** "Inga avvikelser — alla N konton stämmer ✓" centrerat.
- **Loading:** "Hämtar konto-diff…" (samma stil som befintliga `Hämtar data…`).
- **Error:** röd alert-bar (samma stil som CoverageReport top-level).

**Tillgänglighet:** `role="dialog" aria-modal="true"`, fokus-fälla, första fokusbara element = stäng-knappen, Escape stänger, focus-return till klickad rad.

### b) `CoverageReport.tsx` (uppdaterad)

- Lägg till state `selection: {...} | null`, initial `null`.
- I drill-listans tabell: gör varje `<tr>` klickbar (`role="button"`, Enter/Space-handler), `onClick` sätter `selection`. Tooltip "Visa per-konto-diff".
- Rendera `<CoverageAccountsDrawer selection={selection} onClose={() => setSelection(null)} />` sist i komponenten.
- Scroll-lock på body när drawer är öppen (nytt beteende — finns inte i komponenten idag).

## 6. Edge cases, tester & utrullning

**Edge cases att hantera explicit:**

- **Januari (period `YYYY01`) för SIE/SAFT:** `SUM(...) OVER (... ORDER BY period)` startar om vid årsgräns (partition på `FLOOR(period/100)`), så januari blir bara januari-rader — korrekt eftersom backup också är YTD-from-jan.
- **Konton som bara finns i en källa:** FULL JOIN i `account_diff` ger `only_facit` resp. `only_fact` — räknas som mismatch. Inte filtrera bort konto 8999/9999/237X på SQL-nivå (de utesluts redan av `process_*`-skripten på fact-sidan; om de dyker upp i facit-sidan är det en riktig mismatch).
- **Account_name-skillnader:** `COALESCE(backup.account_name, fact.account_name)` — backup vinner.
- **Decimal-precision:** `ROUND(diff, 2)` innan tröskeltest.
- **Bolag utan facit (Norge):** `backup_from_mercur` saknar dem — matris-status `'extra'` (utan facit alls) trumfar `'mismatch'`. Drilldown returnerar då fact-radernas konton som `only_fact` så användaren ser att det är facit som saknas, inte fact.

**Verifiering före merge:**

- Snapshot `compare_coverage` före+efter på en känd 202604-bolagsmix (helst inkluderar 134/146/196 där vi vet sanningen). Räkna mismatch-celler per källa — förväntat: SIE/SAFT-mismatch går från 0 till >0 där facit faktiskt avviker; **IMP-mismatch kan öka markant** eftersom per-konto-jämförelse fångar compensating errors som summerar till noll-diff (något sum-testet missar idag). Räkna även **query-tid** före+efter — `account_diff` materialiseras per konto (O(bolag × period × källa × konto) ≈ 50–100× fler rader än dagens aggregat), så bekräfta att Postgres-planern CSE:ar CTE:n och att matris-svaret stannar under 1 s.
- Manuell drilldown på två kända fall: ett bolag med känd mismatch (matchar Mercur-rapport-diff) + ett bolag som ska vara rent.
- Inga DB-migrationer — bara nytt SQL i `webapp/backend/sql/`. Backend hot reload räcker. Frontend rebuildas separat.

**Out of scope (denna PR):**

- YTD-jämförelse av **IMP** (FI/DK/DE) — fortsätter vara monthly↔monthly. Kan generaliseras senare om behov.
- Sortering/filtrering bortom default i drawer.
- Export av diff-listan.
- Histogram över `|diff|` på matrisen.

---

## Addendum 2026-05-18: empiriska upptäckter under Task 1

Prototyperingen mot live DB avslöjade tre constraints som specen ovan inte fångade. Justeringar nedan är godkända av projektägaren.

### A1. Sign-konvention mellan backup_from_mercur och fact_balances (SIE/SAFT)

**Upptäckt:** `backup_from_mercur` lagrar SIE/SAFT-data i **Mercur-konvention** (intäkt positiv, kostnad negativ), medan `fact_balances` lagrar SIE/SAFT i **SIE-konvention** (intäkt negativ, kostnad positiv). Empirisk verifiering (54 075 SIE-konto-perioder, 2 897 SAFT): 96 % respektive 90 % matchar exakt med sign-flippad jämförelse, mot 0,1 % respektive 0,8 % utan flip. För IMP/MAN/IMP_ADJ är båda sidor Mercur-konvention — ingen flip behövs.

**Justering:** Multiplicera `backup_from_mercur.amount` med `-1` i SIE/SAFT-grenen av `account_diff` (både `compare_coverage.sql` och `coverage_accounts.sql`). Window-SUM:en blir `SUM(-amount) OVER (...)`. IMP-grenen oförändrad.

### A2. Mercur har grövre kontoplan än SIE/SAFT-källfilerna

**Upptäckt:** För SE-bolag 32 har `fact_balances.SIE` 209 distinkta konton för 202604, medan `backup_from_mercur.SIE` har 131 (efter YTD-cum). Diffen är systematisk — Mercur aggregerar bolagskonton till sin egen rapport-kontoplan. Det betyder att `only_fact` (konton som finns i fact men inte i backup) är **förväntat** för SIE/SAFT, inte ett tecken på dataproblem.

**Justering:** För SIE/SAFT räknas `only_fact` **inte** som mismatch på matris-nivå (EXISTS-testet i `compare_coverage.sql`). I drilldown-drawer visas raderna fortfarande som-är (informativt — användaren ser att Mercur saknar dem). För IMP/MAN/IMP_ADJ är `only_fact` oförändrat ett mismatch-tecken (samma kontoplan på båda sidor).

### A3. BS-konton kräver IB för korrekt YTD-jämförelse (SIE/SAFT)

**Upptäckt:** För BS-konton (1xxx, 2xxx) är `SUM(monthly från jan)` = **förändring sedan årsstart**, inte slutsaldo. Slutsaldo = IB + SUM. `fact_balances.SIE/SAFT` lagrar däremot slutsaldo (YTD). Diff:en mellan dem är därför ungefär = ingående balans för året, vilket inte är "amount_diff" utan en strukturell konsekvens.

**Praktisk begränsning:** Endast 9 av 55 SE-bolag och 30 av 39 NO-bolag har IB-rader (`source_kind='IB'`, period 202112) i `fact_balances`. Att lägga till IB för resterande bolag är utanför scope för denna iteration (kräver export från Mercur för förvärv som skett efter 202112).

**Justering:** För SIE/SAFT klassas BS-konton som ny status `no_baseline` i `coverage_accounts_diff` (i stället för att riskera falska `amount_diff`-flaggor). Definieras som: `LEFT(account_code, 1) IN ('1', '2')` AND `source_kind IN ('SIE', 'SAFT')`. I matris-EXISTS-testet räknas `no_baseline` **inte** som mismatch. I drawer visas BS-rader för SIE/SAFT med tooltip "BS-saldo kan inte jämföras utan IB — visas som information". IMP/MAN/IMP_ADJ oförändrat (BS-konton där är monthly-bevegelser mot monthly och jämförs direkt).

**Uppdaterad `status_acc`-enum:** `'ok' | 'amount_diff' | 'only_facit' | 'only_fact' | 'no_baseline'`.

**Uppdaterat matris-EXISTS-test (compare_coverage.sql):**
```sql
WHEN EXISTS (
    SELECT 1 FROM account_diff ad
    WHERE ad.company_id  = COALESCE(b.company_id, f.company_id)
      AND ad.period      = COALESCE(b.period, f.period)
      AND ad.source_kind = COALESCE(b.source_kind, f.source_kind)
      AND ad.scenario    = COALESCE(b.scenario, f.scenario)
      AND (
        -- IMP/MAN/IMP_ADJ: alla felstatus räknas (samma kontoplan)
        (ad.source_kind IN ('IMP','MAN','IMP_ADJ')
         AND ad.status_acc IN ('amount_diff','only_facit','only_fact'))
        -- SIE/SAFT: bara amount_diff + only_facit (only_fact = grövre Mercur-plan,
        --          no_baseline = BS utan IB — båda är förväntade artefakter)
        OR (ad.source_kind IN ('SIE','SAFT')
            AND ad.status_acc IN ('amount_diff','only_facit'))
      )
) THEN 'mismatch'
```
