# SAF-T: FY-range-skopad period-DELETE (clobber-fix) — designspec 2026-05-29

**Spår skapat ur konsistenskoll-fyndet** (se
`docs/saft-analysis-consistency-audit-2026-05-29.md`). Eget spår eftersom det rör
produktionsloadern `load_saft.load_file` och har en designavvägning, inte en
trivial patch.

## Problem

Efter ValueDate-periodiseringen (b711832) spänner EN SAF-T-fil över många
perioder. `load_file` (icke-override) bygger `journal_periods` ur linjernas
ValueDate med BARA en övre gräns (`jp > period_override`), ingen undre. En fil med
strö-ValueDate-rader i en ANNAN periods månad raderar då den periodens data
(per-period-DELETE) och lägger in bara strö-raderna. Sista skrivaren vinner.

`backfill_file_analysis` ärver samma per-period-DELETE → samma clobber i
analyslagret.

## Bevis (avgör designen)

Bolag 9 (Beslag Consult), period 202203, journal efter laddning:

| transaction_id | transaction_date | konto | belopp | källa |
|---|---|---|---|---|
| 510 | **2022-03-18** | 1740 | -357.47 | `extracted/202604/...2026-4.xml` |
| 510 | 2022-03-18 | 7550 | 357.47 | (samma) |
| … 4 identiska par … | | | | |

Den **2026**-daterade produktionsfilen innehåller **2022-daterade** transaktioner
(Visma "VG" exporterar hela huvudboken), och samma verifikatlinje finns **4 ggr**
(känt SAF-T-dubblettmönster). 202203 ägdes egentligen av FY2022-filen (~3000
rader) men clobbrades till dessa 8.

**Slutsats:** strö-raderna är RE-EXPORT av gammal data, inte nya korrigeringar.
Att unionera filer per period (källfils-skopad DELETE) skulle därför
**dubbelräkna**, inte komplettera. Rätt åtgärd = strö-raderna ska INTE röra
andra FY:s perioder alls.

## Beslut

**Skopa varje fils period-DELETE + insert till filens egna räkenskapsår
`[fy_start, fy_end]`** (redan tillgängligt via `derive_fy_range`, används idag i
override-grenen). Journal-/analyslinjer vars ValueDate-period faller utanför
intervallet droppas (räknas + loggas), de varken raderar eller infogas.

- **No-op för välformade filer** — alla legitima linjer ligger inom filens
  deklarerade FY. ValueDate-spridning (Tripletex årsavskrivning) sker INOM FY och
  påverkas inte.
- **Hindrar clobber** — FY2026-filen rör aldrig 202203 → FY2022-filen behåller
  sin data.
- **Ingen dubbelräkning** — strö-re-export droppas helt, unioneras inte.
- Övre gräns blir `min(fy_end, period_override)` (behåller clean-cut-beteendet),
  undre gräns `fy_start`.

### Avvägning / accepterad risk

En äkta sen korrigering postad i 2026 men valuerad till en tidigare FY skulle
också droppas. Bevisen visar att de utanför-FY-rader vi faktiskt ser är
re-exporter, inte sådana korrigeringar; och alternativet (klampa in i ägande FY)
återinför clobber/dubbelräkning. Drop är säkrast. Om en mjukvara visar sig posta
äkta cross-FY-korrigeringar blir det en framtida förfining (per-software-regel).

### Avgränsning

- **Ingen prod-reload nu.** Kodfixen hindrar återfall och gör en framtida
  korrekt omladdning möjlig; den befintliga clobbrade historiken ligger kvar tills
  ett B1ms-säkert fönster finns (separat beslut). Rapporter använder
  `fact_balances` (YTD, orörd) → opåverkade.
- Bolag 104 (föräldralös 2022-analys) hanteras i punkt 2, inte här.

## Implementation

Rena, DB-lösa funktioner (testbara utan databas):

- `line_rows(..., period_cutoff=None, period_floor=None)` — skip om
  `(period_cutoff and jp > period_cutoff) or (period_floor and jp < period_floor)`.
- `group_analysis_by_period(..., period_floor=None)` — vidarebefordrar.
- `load_file` — `jp_floor = fy_start`, `jp_ceil = min(fy_end, period_override)`;
  använd i pass 1 (journal_periods) och pass 2 (line_rows); räkna+logga
  `journal_out_of_fy`.
- `backfill_file_analysis` — `fy_start, fy_end = derive_fy_range(...)`; skicka
  `period_floor=fy_start, period_cutoff=min(fy_end, period_override)`.

### Tester (TDD)

1. `line_rows` med ValueDate 2022-03 och `period_floor="202601"` → `skipped=True`
   (clobber-regressionsvakt — strö-past-rad).
2. `line_rows` i intervall → ej skippad.
3. `group_analysis_by_period` med [202603 in-FY, 202203 strö] + floor 202601 →
   bara 202603 i output, 202203 saknas.
4. Befintliga `period_cutoff`-tester gröna (oförändrat övre beteende).

Regressionsoraklet (`scripts/saft_regression_oracle.py`) körs mot reala filer —
fingerprinten ska vara additiv (out-of-FY-drop ändrar bara strö-rader).
