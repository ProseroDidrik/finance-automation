# Design: SIE-dimensioner i finance-warehouse

**Datum:** 2026-05-29
**Gren/worktree:** `worktree-sie-dimensions` (off `origin/main`)
**Status:** Godkänd datamodell — redo för implementationsplan.
**Mall:** `docs/superpowers/specs/2026-05-28-saft-dimensions-design.md` (SAF-T, live i prod).

## Bakgrund & mål

SAF-T-dimensionerna är byggda och live i prod (PR #27/#29/#30): generiska
`dim_analysis_type` + `dim_analysis_member` + `fact_saft_analysis`. SIE-vägen
persisterar idag **inga** dimensioner:

- `#DIM <nr> "<namn>"` (dimensionsaxel) och `#OBJEKT <dimnr> "<objektnr>" "<namn>"`
  (medlem) parsas inte.
- Objektlistan `{dim objekt …}` i `#TRANS` kastas bort (`sie_parser.RE_TRANS`
  matchar `\{[^}]*\}` utan capture).
- Dim-suffixade `#PSALDO`-rader skippas medvetet (`RE_PSALDO` matchar bara
  `{}`-totalen — se [[reference_sie_psaldo_dim_filter]]).

Målet: fånga SIE-dimensionerna och göra dem frågbara per månad / YTD / helår /
LTM, brutet på dimensionsaxel och medlem — samma modell och semantik som SAF-T.

### Scope-beslut (låsta)

- **Återanvänd de generiska dim-tabellerna** `dim_analysis_type` +
  `dim_analysis_member` med `source_format='SIE'`. Ny faktatabell
  `fact_sie_analysis` som speglar `fact_saft_analysis`.
- **Källa = `#TRANS` (journalnivå).** En rad per (`#TRANS`-linje × dim-par),
  belopp = linjens belopp, period = verifikatets månad. `#PSALDO`-dim-rader
  väljs bort (annan semantik, en dim/rad, egen period-härledning — bryter
  speglingen av `fact_saft_analysis`).
- **IMP / IMP_ADJ / MAN dimensioneras inte** (IMP saknar dim-data; MAN/IMP_ADJ:s
  dim-kolumner var tomma i praktiken — bedömt 2026-05-29).
- Ingen FK fakta→dim (dim best-effort, upsert `ON CONFLICT`). Ingen UNIQUE på
  linjenyckeln — idempotens via company+period-DELETE.

### Empiriskt underlag

`#TRANS`-objektlistan är par av (dimensionsnr, objektnr), 0..N par per linje:

```
#TRANS 7830 {"1" "100" "2" "300" "6" "9000300"} 1247.27 20260131 "Avskr" 1
#TRANS 1209 {} -1247.27 20260131 "Ack" 2
```

- Tokens kan vara citerade (`{"1" "100"}`) eller ociterade (`{1 "2"}`).
- En dimension kan **inte** upprepas inom en `#TRANS` (SIE 4B: en transaktion
  knyts till högst ett objekt per dimension) → `(linje, dim)` är unikt, precis
  som SAF-T:s `(linje, analysis_type)`. En-typ-filter är därmed exakt.
- Namnen finns i `#DIM`/`#OBJEKT`-deklarationerna (kan saknas → tom dim-tabell).

---

## 1. Datamodell — 1 ny tabell + 1 sekvens (dim-tabellerna återanvänds)

Skapas i `db.py` `SCHEMA_SQL` (`CREATE … IF NOT EXISTS`), enligt konventionen i
`db/migrations/README.md` (tabeller i db.py; migrationer rör roller/rättigheter).

### `dim_analysis_type` / `dim_analysis_member` — oförändrade (delade)

Befintliga tabeller. SIE fyller dem med `source_format='SIE'`:

- `analysis_type` = `#DIM`-nummer (t.ex. `"1"`, `"6"`).
- `analysis_id`   = `#OBJEKT`-objektnummer (t.ex. `"100"`, `"9000300"`).
- `description`   = `#DIM`-namn resp. `#OBJEKT`-namn (nullable).

Upsert `ON CONFLICT (PK) DO UPDATE SET description = EXCLUDED.description,
loaded_at = EXCLUDED.loaded_at`.

### `fact_sie_analysis` — ny (speglar `fact_saft_analysis`)

| kolumn | typ | not |
|---|---|---|
| id | BIGINT PK | `DEFAULT nextval('seq_fact_sie_analysis')`, utelämnas i insert |
| company_id | INTEGER NOT NULL | |
| period | TEXT NOT NULL | **verifikatets månad** (`v["date"][:6]`) = `fact_journal_sie`-period |
| series | TEXT | `#VER`-serie ┐ |
| voucher_number | TEXT | `#VER`-nr  ├ join tillbaka till `fact_journal_sie` |
| line_no | INTEGER NOT NULL | `#TRANS` radnr ┘ |
| account_code | TEXT NOT NULL | |
| analysis_type | TEXT NOT NULL | `#DIM`-nr |
| analysis_id | TEXT NOT NULL | `#OBJEKT`-nr |
| amount | DOUBLE PRECISION NOT NULL | `#TRANS`-beloppet (månadsrörelse, linjenivå) |
| currency | TEXT NOT NULL | SEK / `#VALUTA` |
| source_file | TEXT NOT NULL | |
| loaded_at | TIMESTAMP NOT NULL | |

Index (speglar SAF-T): `(company_id, period)`, `(company_id, analysis_type,
analysis_id)`, `(account_code)`, `(period)`. Ny sekvens `seq_fact_sie_analysis`.

- **Separat tabell** (inte delad `fact_analysis`) — följer "formaten blandas
  aldrig" (jfr `fact_journal_sie` vs `fact_journal_saft`). SAF-T-lagret rörs ej.
- Naturlig join-nyckel `(company_id, period, series, voucher_number, line_no)` →
  `fact_journal_sie`. Analog till SAF-T:s `(transaction_id, line_no, record_id)`.
- Ingen UNIQUE på linjenyckeln; idempotens via company+period-DELETE.

---

## 2. Periodisering — KRITISKT: verifikatets månad (samma som journalen)

> Detta är SIE-motsvarigheten till SAF-T:s ValueDate-landmina
> ([[reference_saft_valuedate_bug]]). `fact_journal_sie` periodiseras på
> **verifikatdatumet** (`v["date"][:6]`) — `vouchers_to_journal_rows` ignorerar
> `#TRANS`:ens valfria egna `transdat`. Analysraderna byggs över **samma linjer i
> samma loop** och måste ärva **exakt samma period** — annars uppstår en
> b711832-liknande divergens i ett nytt lager.

**Bindande krav:**

1. Analys-tupler emitteras **inuti `vouchers_to_journal_rows`** (mirror av
   SAF-T:s `line_rows` som returnerar både journal- och analys-tupler från en
   period-härledning). Period sätts aldrig från `#PSALDO` eller från `#TRANS`:ens
   egen `transdat` — alltid `v["date"][:6]`.
2. Samma `period_cutoff` (`--period`-gräns: vouchers vars period > cutoff
   skippas) tillämpas på journal **och** analys i samma skip → `analysis_periods
   ⊆ journal_periods` by construction.
3. Per-period-DELETE för `fact_sie_analysis` använder **samma `journal_periods`-
   set** som journal-DELETE:n — inget separat uträknat set.

**Granularitet:** en rad per (`#TRANS`-linje × dim-par) med **en månads-period**.
Lagra aldrig YTD-aggregat — månadsrörelse per linje är basen; enskild månad / YTD
/ helår / LTM blir rena period-range-filter (samma rollup-tabell som SAF-T:
`WHERE period = …` / `BETWEEN … AND …`).

---

## 3. Parser-ändringar (`sie_parser.py`) — två kontraktsändringar

- **`parse_sie` läser dim-deklarationer:**
  - `#DIM <nr> "<namn>"` → `out["dims"]` (lista av `(dim_nr, namn)`).
  - `#OBJEKT <dimnr> "<objektnr>" "<namn>"` → `out["objekt"]`
    (lista av `(dim_nr, objekt_nr, namn)`).
  - Måste tåla att de saknas (tom lista) — många SE-bolag dimensionerar inte.
- **`RE_TRANS` fångar objektlistan:** ändra `\{[^}]*\}` → `\{([^}]*)\}` och lägg
  `"analysis"`-nyckel (lista av `(dim, objekt)`-par) på varje trans-dict.
  - **Gruppförskjutnings-fälla (måste hanteras):** den nya capture-gruppen
    skjuter belopp/transdat/text/quantity från grupp 2/3/4/5 → 3/4/5/6.
    `parse_sie` läser idag `m.group(2)` (belopp), `m.group(4)` (trans_text),
    `m.group(5)` (quantity). **Lösning: konvertera `RE_TRANS` till namngivna
    grupper** (`(?P<amount>…)` etc) så positioner inte kan glida — eller renumrera
    varje `m.group()`. Namngivet föredras.
  - **Brace-tokenisering:** par av (dim, objekt); tokens citerade eller ej.
    Udda token-antal (defekt objektlista) → skippa paret + WARN, krascha inte.
  - **Bevarad garanti:** tal i braces (`9000300`) får aldrig läcka in i `amount`
    (test `test_multidim_brace_does_not_leak_into_amount`,
    `test_voucher_with_dims_still_balances`, Visma- och quantity-testerna är
    regressionsvakten — måste förbli gröna).

---

## 4. Loader-ändringar (`load_sie.py`)

- **Upsert dim:** en `sie_dim_analysis_rows(parsed["dims"], parsed["objekt"], …)`-
  builder normaliserar SIE:s **två** listor (`#DIM` axlar, `#OBJEKT` medlemmar)
  till samma `(type_rows, member_rows)`-form som SAF-T:s `dim_analysis_rows`
  (egen builder — inte drop-in, eftersom SAF-T matar en flat tuple-lista).
  `source_format='SIE'`. Upserta `dim_analysis_type` + `dim_analysis_member`.
- **Analys-fakta:** `vouchers_to_journal_rows` returnerar även analys-tupler
  (en per `#TRANS`-linje × dim-par) med linjens journal-period. Insert via samma
  `executemany`-batch-stil (`JOURNAL_BATCH`) som `fact_journal_sie` — håller
  `load_sie.py` internt konsekvent; SIE-volymer är mindre än Actas (ingen COPY).
- **Idempotens-paritet:** spegla *båda* journal-DELETE:arna för
  `fact_sie_analysis`:
  - per-period: `WHERE company_id=%s AND period IN (<journal_periods>)`
    (samma set som journal-DELETE).
  - FY-override: `WHERE company_id=%s AND period > <filens period>
    AND period BETWEEN fy_start AND fy_end` (speglar journal-FY-DELETE i
    override-grenen).

  Annars ackumulerar `--override` dubbletter tyst.
- Följer `--include-journal` (ingen journal → inga dims; ingen egen flagga).
- `init_schema` redan guardad mot `InsufficientPrivilege` (etl-rollen utan DDL)
  — oförändrat.

---

## 5. Semantik & avstämning (`docs/warehouse_semantics.md` + `describe_schema`)

Nytt mental model (SIE-spegling av SAF-T:s femte fälla). `fact_sie_analysis` är
en per-(linje, dim)-explosion:

1. **SUM:a aldrig över `analysis_type`** — multi-dim upprepar hela linjebeloppet.
   Filtrera alltid på **en** `analysis_type`.
2. **Odimensionerad rest:** täckning < 100% (bolag/konton utan objektlista, och
   bolag med tunna `#VER` — t.ex. löner saknas i `#VER` för 14/18/41/152, se
   [[reference_sie_ver_hybrid_fallback]]). `SUM(amount WHERE type=X) ≤ journal-
   total` — resten är otaggad. Inga placeholder-rader.
3. **`amount` är linjenivå → alltid månadsrörelse**, aldrig YTD. SUM:a aldrig
   analysbelopp mot `fact_balances` (YTD för SE/NO) — det ger nonsens. Använd
   `fact_journal_sie` (månadsrörelse) som jämförelsebas.
4. En dimension upprepas aldrig inom en `#TRANS` → en-typ-filter är exakt.

---

## 6. Tester (TDD, speglar SAF-T:s upplägg)

- **Enhet (ingen DB, `unittest`):**
  - `parse_sie` läser `#DIM`/`#OBJEKT` (med/utan → tom lista).
  - `#TRANS` `analysis`-extraktion: 0/1/N dim-par, citerade + ociterade tokens,
    udda token-antal → skip + WARN.
  - **Brace-no-leak + voucher-balans bevarade** (befintliga tester gröna).
  - `sie_dim_analysis_rows` builder: två listor → dedupliderade type/member-rader.
- **Orakel:** regenerera journal-golden (`--capture`), diffa, bekräfta enda
  ändring = `analysis`-nyckeln (inget annat journalfält rört).
- **Integration (lokal `finance-pg-dev`, dev:dev@localhost — RÖR ALDRIG PROD):**
  - ladda dim-tung SIE → `dim_analysis_*` fyllda (source_format='SIE'),
    `fact_sie_analysis`-radantal = Σ dim-par, "sum per en typ ≤ journalsubset",
    `--override` → 0 dubbletter.
  - **Periodparitets-vakt (regressionsvakt för §2):** analysens periodfördelning
    **==** `fact_journal_sie`:s periodfördelning för samma bolag. Detta är testet
    som fångar en återinförd b711832-liknande bugg i dimensionslagret.

---

## 7. Migration & utrullning

- **Tabell + sekvens:** `db.py` `SCHEMA_SQL` (`CREATE … IF NOT EXISTS`).
- **Grants:** `db/migrations/20260529_sie_analysis_grants.sql` (datum justeras vid
  faktisk körning) — `GRANT SELECT` → `mcp_readonly` (ingen PII → full SELECT OK),
  `GRANT INSERT/DELETE/SELECT` → `etl_writer`. Följer T2/T3-mönstret.
- **Prod-ordning (dokumenteras, körs EJ i detta arbete):** admin `py db.py` (DDL)
  → applicera grants-migration → ETL-reload (`load_sie.py --period … --override`).
  Lokal-först; prod är Didriks beslut.
- **Uppföljning:** ladda om SIE-historik en gång via
  `load_history_sie_saft.py --format sie` när dims är byggda (jfr SAF-T-backfill).

---

## 8. Delade filer att samordna vid merge

`db.py` (SCHEMA_SQL), `SCHEMA.md`, `docs/warehouse_semantics.md`,
`db/migrations/`. SAF-T-spåret är mergat (PR #27/#29/#30) så parallellt arbete är
tryggt; rebasea/lös försiktigt.

## 9. Utanför scope (YAGNI)

- `#UNDERDIM` / dim-hierarki (platt modell).
- `#PSALDO`-dim-rader (vald bort till förmån för `#TRANS`).
- Reporting-vyer / pivot-integration (rålager + MCP-semantikdoc räcker först).
- Ingen FK-tvång fakta→dim.
