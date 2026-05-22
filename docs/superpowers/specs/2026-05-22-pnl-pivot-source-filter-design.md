# Design: källfilter-dropdown i P&L-pivoten

**Datum:** 2026-05-22
**Status:** Godkänd design — redo för plan
**Gren:** `worktree-pnl-pivot-source-filter`

## Bakgrund

GUI:ts "P&L"-flik renderar `PnlPivot` (pivot-vyn), som anropar
`/api/report/pivot` → `report_pivot.sql`. Den enskilda `PnlReport.tsx` /
`report_pnl.sql` / `/api/report/options` är död kod (exporteras men renderas
aldrig) och berörs inte av den här ändringen.

`report_pnl.sql` blev additiv 2026-05-21 (utfall = bas-källa + MAN-A +
IMP_ADJ-A). `report_pivot.sql` fick aldrig samma fix:

- `best_source` har `IMP_ADJ` med i prioritetslistan, men bara som sista
  utväg — den väljs aldrig ovanpå en bas-källa.
- `MAN` finns inte alls i `report_pivot.sql`.
- `raw_balances` joinar enbart på `bs.source_kind = fb.source_kind` och saknar
  det additiva OR-villkoret.

Följden: pivot-vyn visar bas-källan men tappar både MAN- och IMP_ADJ-lagret.

## Mål

Lägga till en dropdown med tre kryssrutor i pivot-filterpanelen så användaren
kan styra vilka **lager** som summeras in i utfallet:

- **Baskälla (auto)** — den per land prioriterade bas-källan (SIE/SAFT/IMP).
- **MAN-justeringar** — `source_kind = 'MAN'`, scenario A.
- **IMP_ADJ-justeringar** — `source_kind = 'IMP_ADJ'`, scenario A.

Default: alla tre på (= korrekt fullt utfall). Lager-modellen valdes framför en
flat lista av varje `source_kind` eftersom bas-källorna är ömsesidigt
uteslutande och varierar per bolag i land-vy — en flat lista skulle vara
meningslös att kombinera.

## Scope

| Berörs | Berörs inte |
|--------|-------------|
| `webapp/backend/sql/report_pivot.sql` | `report_pnl.sql` (redan additiv) |
| `webapp/backend/main.py` (`/api/report/pivot`) | `PnlReport.tsx` (död kod) |
| `webapp/frontend/src/api.ts` | `/api/report/options` (död kod) |
| `webapp/frontend/src/components/PnlPivot.tsx` | |
| `scripts/smoke_test_sql.py` | |

## Lösning — Approach 1 (en SQL-fråga med lager-filter-parameter)

Fixa `report_pivot.sql` så den blir additiv (speglar `report_pnl.sql`) och lägg
samtidigt till en lager-filter-parameter. Ett anrop, ingen extra payload.
Pivot-vyn laddar redan om vid varje filterändring och visar "uppdaterar…", så
omladdning vid lager-toggle är konsekvent med befintlig UX.

Förkastade alternativ:
- **Separata anrop per lager, summera i frontend** — 3× HTTP-anrop och
  summeringslogik på fel ställe.
- **Returnera alla lager uppdelade, filtrera i frontend** — bloatar API-svaret
  och kräver schemaändring; overkill för tre kryssrutor.

## Detaljerad design

### 1. SQL — `report_pivot.sql` (tre relaterade ändringar)

Detta är en buggfix + en följdfix + filtret — inte bara ett filter.

**1a. Gör additiv (buggfix).** Ta bort `IMP_ADJ`-raden ur varje gren i
`best_source`-CASE:t (nuvarande rad 70, 79, 88, 94). `best_source` väljer då
bara äkta bas-källor: `SIE_PSALDO` / `SIE_VER` / `SIE` / `SAFT` / `IMP`.

**1b. Fixa `period_type`-hanteringen (följdfix — krävs för korrekthet).**
`raw_balances` grupperar idag bort `period_type` med `MAX(fb.period_type)`.
Det är säkert bara så länge joinen är exklusiv. När MAN/IMP_ADJ-rader (alltid
`monthly`, verifierat mot warehouse 2026-05-22 — 0 `ytd`-rader) får samexistera
med en YTD-bas-rad (SE/NO) för samma (bolag, period, konto) skulle de summeras
ihop och taggas `ytd`, och sedan felaktigt YTD-diffas i `month_amounts`.

Fix: lägg `fb.period_type` i `GROUP BY` i `raw_balances` och välj kolumnen
direkt istället för `MAX(...)`. Då blir det upp till två rader per
(bolag, period, konto): en `ytd` (bas, SE/NO) och en `monthly` (MAN/IMP_ADJ).
`month_amounts` CASE:ar redan på `period_type` per rad; `prev`-self-joinen är
redan begränsad till `prev.period_type = 'ytd'`, så monthly-rader får inget
prev-värde och multipliceras inte. `bucket_amounts` summerar (`SUM`) de två
raderna ihop per bucket — korrekt slutbelopp. För IMP-länder (bas = `monthly`)
hamnar bas + justeringar i samma grupp och summeras direkt.

**1c. Lager-filter.** Byt `raw_balances`-joinen mot `best_source` från
`AND bs.source_kind = fb.source_kind` till tre booleska parametrar:

```sql
JOIN best_source bs
  ON bs.company_id = fb.company_id
 AND bs.period     = fb.period
 AND (
      (%s::boolean AND bs.source_kind = fb.source_kind)
   OR (%s::boolean AND fb.source_kind = 'MAN')
   OR (%s::boolean AND fb.source_kind = 'IMP_ADJ')
 )
```

OR i join-villkoret avgör bara *om* en `fb`-rad joinar, inte hur många gånger —
ingen radmultiplicering även om flera disjunkt:er är sanna.

**Ny bind-param-ordning** (efter `{bucket_values}`-substitutionen):

```
bucket-värden (3 per bucket)
company_ids        -- company_filter UNNEST
source_kind        -- best_source COALESCE
include_base       -- raw_balances join (NY)
include_man        -- raw_balances join (NY)
include_imp_adj    -- raw_balances join (NY)
scenario           -- raw_balances WHERE
report_currency    -- month_amounts_fx
```

Header-kommentaren i `report_pivot.sql` uppdateras med den nya ordningen och
en notering om att `IMP_ADJ` tagits bort ur `best_source`.

### 2. Backend — `/api/report/pivot` i `main.py`

Ny query-param `source_layers: str` (komma-separerad), default
`"base,man,imp_adj"`. Parsas till tre booleans:

```python
layers = {s.strip() for s in source_layers.split(",") if s.strip()}
include_base    = "base"    in layers
include_man     = "man"     in layers
include_imp_adj = "imp_adj" in layers
```

`params` byggs om så de tre boolean-värdena hamnar mellan `source_kind` och
`scenario`:

```python
params = (
    bucket_params
    + [company_ids_list, source_kind,
       include_base, include_man, include_imp_adj,
       scenario, report_currency]
)
```

`source_kind`-paramen (hård override av bas-källan) lämnas orörd och är
ortogonal mot lager-filtret.

### 3. `api.ts`

```ts
export type SourceLayer = "base" | "man" | "imp_adj";

export interface PivotQuery {
  // ... befintliga fält ...
  source_layers?: SourceLayer[];
}
```

`fetchPivot` appendar `source_layers` när satt:
`params.append("source_layers", q.source_layers.join(","))`.

### 4. Frontend — `PnlPivot.tsx`

Ny dropdown som speglar befintliga **"Kolumner"**-pickern (nuvarande rad
463–488 — samma button + absolut-positionerad checkbox-panel).

```
Filterrad:
[Bolag ▾] [202601→202604] [Månad|Kvartal|Halvår|År] [Valuta ▾] \
  [YTD|LTM|Budget] [Källor (3/3) ▾] [Kolumner (8/8) ▾]   Expand all  Collapse

Dropdown vid klick på "Källor":
┌───────────────────────────┐
│ ☑ Baskälla (auto)         │
│ ☑ MAN-justeringar         │
│ ☑ IMP_ADJ-justeringar     │
└───────────────────────────┘
```

- Nytt state `sourceLayers: { base: boolean; man: boolean; imp_adj: boolean }`,
  default alla `true`. Eget `showSourcePicker`-state för dropdown-öppning.
- `sourceLayers` läggs i `useEffect`-deps-listan → rapporten laddas om vid
  toggle (konsekvent med period/granularitet/valuta).
- Actuals-anropet (`actualP`, scenario A) får `source_layers` härlett ur
  `sourceLayers`-state.
- Budget-anropet (`budgetP`, scenario B, `source_kind: "MAN"`) får hårdkodat
  `source_layers: ["base"]`, med inline-kommentar:
  *"Budget = scenario B med forcerad source_kind=MAN; lager-filtret styr bara
  scenario A:s utfall. Vi skickar ["base"] explicit så att en eventuell
  scenario-B IMP_ADJ inte smyger in i budgetkolumnen."*
- Ingen persistens — komponent-state, precis som `hiddenBuckets`.
- Alla tre rutor avbockade → tomt utfall; befintliga "Ingen data för valt
  urval"-raden täcker fallet.
- Placeras precis före "Kolumner"-pickern i filterpanelen.

## Verifiering / acceptanskriterier

1. **Additiv korrekthet:** med alla tre lager på ska `report_pivot.sql`:s
   månadsbelopp per konto för ett SE-bolag *och* ett FI-bolag (båda med känd
   MAN/IMP_ADJ-data) matcha `report_pnl.sql`:s `amount_month` för samma
   (bolag, period). Det är facit för "additiv gjord rätt".
2. **Lager-toggle:** avbockning av MAN respektive IMP_ADJ ska sänka beloppen
   med exakt det lagrets bidrag; avbockning av Baskälla ska lämna bara
   justeringslagren.
3. **Smoke-test:** `scripts/smoke_test_sql.py` uppdateras med det nya
   param-antalet för `report_pivot.sql` och körs grönt mot Azure-DB innan
   commit (per projektets SQL-smoke-test-rutin).
4. **Budget oförändrad:** Budget-kolumnen (scenario B) ska ge samma siffror
   före och efter ändringen.

## Risker

- **Param-ordning:** `report_pivot.sql` använder positionsbundna `%s`. Fel
  ordning ger tyst felaktiga siffror. Mitigeras av smoke-testet och
  acceptanskriterium 1.
- **`period_type`-följdfixen** är den subtila delen — utan den blir SE/NO-tal
  fel först när MAN/IMP_ADJ faktiskt finns för perioden. Acceptanskriterium 1
  med ett SE-bolag fångar det.
