# Design: Dimensioner i finance-warehouse (SAF-T först)

**Datum:** 2026-05-28
**Gren/worktree:** `worktree-dimensions` (off `origin/main`)
**Status:** Godkänd datamodell — redo för implementationsplan.

## Bakgrund & mål

SAF-T-/SIE-refaktorn (Etapp 0–4, PR #25) konsoliderade parsningen till
`saft_parser.py` (NO+DK, iterparse) och `sie_parser.py` (SE). Idag persisteras
**inga dimensioner**:

- **SAF-T:** `<Analysis>` per `<Line>` (`AnalysisType` + `AnalysisID`,
  t.ex. kostnadsställe/avdelning/projekt) ignoreras helt — avvikelse B3 i
  `docs/saft-etapp3-kartlaggning-2026-05-28.md`.
- **SIE:** `#DIM`/`#OBJEKT` (refererade via `{dim "objekt"}` i `#TRANS`/`#PSALDO`)
  persisteras inte; `load_sie.py` skippar t.o.m. dim-suffixade `#PSALDO`-rader.

Målet är att fånga dimensionerna och göra dem frågbara per
månad / YTD / helår / LTM, brutet på dimensionsaxel och medlem.

### Scope-beslut

- **Enhetlig modell, SAF-T först.** Dimensionstabellerna designas så att de
  konceptuellt rymmer både SAF-T `Analysis` och SIE `#DIM`/`#OBJEKT`, men endast
  SAF-T-vägen implementeras och laddas nu. SIE blir ett dokumenterat följdsteg
  som återanvänder samma schema.
- **Bara dimensioner.** Ingen Analysis-bredd dras in (TaxInformation,
  CurrencyCode/ExchangeRate-detalj, SourceDocumentID m.fl. B3-fält).

### Empiriskt underlag (riktiga 202604-filer)

`Line.Analysis` = `{AnalysisType, AnalysisID}`, **0..N block per linje**:

- **NO 009 (Beslag-Consult):** Analysis på 98% av linjerna; typer som `DEP`
  (Avdeling), `PRO` (Prosjekt). Enstaka block per linje.
- **DK 081 (Actas, 134 165 linjer):** dimensions-**rik** — upp till 9 block per
  linje (vanligast 4–6); koder `VoTp` (verifikattyp) + `OrgUnit1..12`.
  **Verifierat över alla 134 165 linjer: samma `AnalysisType` upprepas aldrig
  inom en linje** → `(linje, analysis_type)` är unikt.

Namnen finns gratis i `MasterFiles/AnalysisTypeTable/AnalysisTypeTableEntry`:
`AnalysisType` + `AnalysisTypeDescription` + `AnalysisID` + `AnalysisIDDescription`
+ `Status`. (NO 009: 1 tabell; DK 081: 83 915 entries.) Tabellen kan saknas på
NO 1.20 / DK 1.0 → måste tålas.

---

## 1. Datamodell — 3 nya tabeller

Tabellerna skapas i `db.py` `SCHEMA_SQL` (`CREATE … IF NOT EXISTS`), enligt
konventionen i `db/migrations/README.md` ("tabeller skapas i db.py; migrationer
rör roller/rättigheter/vyer").

### `dim_analysis_type` — dimensionsaxeln (delad, unified-ready)

| kolumn | typ | not |
|---|---|---|
| company_id | INTEGER NOT NULL | axlar är bolagslokala |
| source_format | TEXT NOT NULL | `'SAFT'` nu, `'SIE'` senare |
| analysis_type | TEXT NOT NULL | `DEP`, `PRO`, `OrgUnit1`, `VoTp`… (SIE: `#DIM`-nr) |
| description | TEXT | `AnalysisTypeDescription` (nullable) |
| loaded_at | TIMESTAMP NOT NULL | |
| **PK** | (company_id, source_format, analysis_type) | |

### `dim_analysis_member` — medlemmen/objektet

| kolumn | typ | not |
|---|---|---|
| company_id | INTEGER NOT NULL | |
| source_format | TEXT NOT NULL | |
| analysis_type | TEXT NOT NULL | |
| analysis_id | TEXT NOT NULL | `AnalysisID` (SIE: `#OBJEKT`-objektnr) |
| description | TEXT | `AnalysisIDDescription` (nullable) |
| loaded_at | TIMESTAMP NOT NULL | |
| **PK** | (company_id, source_format, analysis_type, analysis_id) | |

Båda fylls ur `AnalysisTypeTable` (flat entry → deduplicerad till de två
nivåerna). SIE:s `#DIM` (axel) + `#OBJEKT` (medlem) mappar 1:1 senare.

- **Ingen FK fakta→dim** — dim är best-effort namnslagning. `Line.Analysis` kan
  referera en `(type,id)` som saknas i `AnalysisTypeTable`, och tabellen kan
  saknas helt. Fakta är källa till sanning.
- Upsert `ON CONFLICT (PK) DO UPDATE SET description = EXCLUDED.description,
  loaded_at = EXCLUDED.loaded_at`.

### `fact_saft_analysis` — analys-faktan (en rad per `Line` × `Analysis`-block)

| kolumn | typ | not |
|---|---|---|
| id | BIGINT PK | `DEFAULT nextval('seq_fact_saft_analysis')`, **utelämnas i COPY** |
| company_id | INTEGER NOT NULL | |
| period | TEXT NOT NULL | **ValueDate-härledd per linje — se §2** |
| transaction_id | TEXT | naturlig linjenyckel (join tillbaka vid behov) |
| line_no | INTEGER NOT NULL | |
| record_id | TEXT | kan vara NULL |
| account_code | TEXT NOT NULL | → "belopp per konto × dimension" direkt |
| analysis_type | TEXT NOT NULL | |
| analysis_id | TEXT NOT NULL | |
| amount | DOUBLE PRECISION NOT NULL | **linjens belopp** (debit − credit) |
| currency | TEXT NOT NULL | |
| source_file | TEXT NOT NULL | |
| loaded_at | TIMESTAMP NOT NULL | |

Index: `(company_id, period)`, `(company_id, analysis_type, analysis_id)`,
`(account_code)`, `(period)`.

- **Ingen UNIQUE på linjenyckeln** (`record_id` kan vara NULL, `transaction_id`
  ej garanterat unik). Idempotens sköts via company+period-DELETE, inte upsert.
- Ny sekvens `seq_fact_saft_analysis`.

Tabellen bär själv beloppet → "spend per kostnadsställe/projekt" blir
`GROUP BY analysis_type, analysis_id` utan join mot den stora journalen. Detta
speglar SIE-dim-semantiken redan dokumenterad i `sie_parser.py`
(`Σ(dim-rader) = antal dim-typer × {}`).

---

## 2. Periodisering — KRITISKT: ValueDate per linje (samma som journalen)

> Detta är den enda landminan i designen. b711832-historiken
> (`reference_saft_valuedate_bug`): journalen periodiserades fel på
> `TransactionDate` i st f `ValueDate` per linje. Fix b711832 löste det. Eftersom
> analysraderna byggs i **samma pass 2 över samma linjer** måste de **ärva exakt
> samma period-härledning** — annars återinförs buggen i ett nytt lager där den
> är ännu svårare att upptäcka (Tripletex-NO klumpar årets avskrivningar i jan).

**Bindande krav:**

1. Varje analysrad stämplas via **samma `_journal_period(line)`** som
   journalfaktan (`saft_parser.py`):
   ```python
   d = j.get("value_date") or j["transaction_date"]
   period = f"{d.year:04d}{d.month:02d}" if d else fallback
   ```
   Period får **aldrig** sättas från `derive_period()` (filhuvudet) eller från
   `transaction_date` ensamt.
2. Per-period-DELETE för `fact_saft_analysis` använder **exakt samma
   `journal_periods`-set** som journal-DELETE:n (det ValueDate-härledda settet i
   pass 1) — inte ett separat uträknat set.
3. Samma `--period`-cutoff (`jp > period_override` droppas) och samma
   ValueDate-saknas-fallback (`value_date or transaction_date`) tillämpas per
   linje, identiskt med journalen.

**Granularitet:** en rad per (journallinje × analysblock) med **en månads-period**.
Lagra aldrig YTD-aggregat — månadsrörelse per linje är basen; enskild månad / YTD
/ helår / LTM blir rena period-range-filter (rollups):

| Användning | Query |
|---|---|
| Enskild månad | `WHERE period = '202604'` |
| YTD | `WHERE period BETWEEN '202601' AND '202604'` |
| Helår | `WHERE period BETWEEN '202601' AND '202612'` |
| LTM | `WHERE period BETWEEN '202505' AND '202604'` |

---

## 3. Semantik & avstämning (ny *femte* fälla i `describe_schema` + `warehouse_semantics.md`)

`fact_saft_analysis` är en per-(linje,axel)-explosion. Tre regler:

1. **SUM:a aldrig över `analysis_type`** — multi-axel upprepar hela linjebeloppet
   (en DK-linje har upp till 9 axlar). Filtrera alltid på **en** `analysis_type`.
2. **Odimensionerad rest:** täckning < 100% (98% i NO 009; 13 otaggade linjer i
   DK 081). `SUM(amount WHERE analysis_type=X) ≤ journaltotal` — resten är otaggad.
   Förstklassig fälla, **inga placeholder-rader** (det skulle bli rader×axlar).
3. Verifierat: samma typ upprepas aldrig inom en linje → en-typ-filter är
   **exakt** för den axeln (ingen intra-axel-dubbelräkning).

**Periodsemantik-not (intill multi-axel-fällan):** `fact_saft_analysis.amount` är
linjenivå → **alltid månadsrörelse**, aldrig YTD. Till skillnad från
`fact_balances.amount` som är YTD för SE/NO. SUM:a aldrig analysbelopp mot
`fact_balances` YTD — det ger nonsens.

---

## 4. Parser-ändringar (`saft_parser.py`) — två kontraktsändringar

- **`parse_saft`:** läs även `MasterFiles/AnalysisTypeTable/AnalysisTypeTableEntry`
  → `out["analysis_types"]` (lista av `(analysis_type, type_desc, analysis_id,
  id_desc)`). Samma iterparse-pass före `GeneralLedgerEntries`-break. **Måste tåla
  att tabellen saknas** (NO 1.20 / DK 1.0 → tom lista).
  → `tests/test_saft_parser.py` uppdateras (kontraktsändring).
- **`iter_saft_journal`:** lägg till `"analysis"`-nyckel i varje yieldad
  linje-dict (lista av `(analysis_type, analysis_id)`, tom om inga block). Samma
  pass, inga extra fil-genomläsningar.
  → **regressions-oraklet regenereras medvetet** (`--capture` + diff som bevisar
  att *bara* `analysis`-nyckeln tillkommit), egen atomär commit.

---

## 5. Loader-ändringar (`load_saft.py`)

- Upserta `dim_analysis_type` + `dim_analysis_member` ur `parsed["analysis_types"]`
  (`source_format='SAFT'`, per company).
- I journal-pass 2 (COPY-loopen): buffra analys-tupler per linje (en per
  Analysis-block, med linjens `jp` från `_journal_period`, `amount`,
  `account_code`, `currency`, `source_file`). Efter att journal-COPY:n stängts,
  kör en **andra COPY** till `fact_saft_analysis`. (psycopg tillåter en COPY i
  taget per anslutning → buffra; ~580k rader för DK 081 ryms i minne som tupler.)
- **Idempotens-paritet:** spegla *båda* journal-DELETE:arna för
  `fact_saft_analysis`:
  - per-period `WHERE company_id=%s AND period IN (<journal_periods>)`,
  - FY-bred override `WHERE company_id=%s AND period BETWEEN fy_start AND fy_end`.

  Annars ackumulerar `--override` dubbletter tyst.
- Analys följer `--include-journal` (otaggad om journal hoppas över; ingen egen
  opt-out-flagga).

---

## 6. Tester

- **Enhet (ingen DB, `unittest`):** inline-XML med 0/1/N Analysis-block + med/utan
  `AnalysisTypeTable`. Assert:a `parse_saft` läser typtabellen och `iter_saft_journal`
  yieldar rätt `analysis`-lista.
- **Orakel:** regenerera `tests/saft_oracle_golden.json` (`--capture`), diffa,
  bekräfta att enda ändringen är `analysis`-nyckeln (inget annat journalfält rörts).
- **Integration (lokal `finance-pg-dev`, dev:dev@localhost — RÖR ALDRIG PROD):**
  - ladda NO 009 + DK 081 → `dim_analysis_*` fyllda, `fact_saft_analysis`-radantal
    = Σ block, "sum per en typ ≤ journalsubset", `--override` → 0 dubbletter.
  - **Tripletex-divergenstest (regressionsvakt för §2):** för ett NO-bolag där
    `ValueDate` ≠ `TransactionDate` (158 Asker / 189), assert:a att analysens
    periodfördelning **==** journalens periodfördelning. Detta är testet som
    fångar en återinförd b711832-bugg i dimensionslagret.

---

## 7. Migration & utrullning

- **Tabeller + sekvens:** `db.py` `SCHEMA_SQL` (`CREATE … IF NOT EXISTS`).
- **Grants:** `db/migrations/20260528_analysis_dimension_tables.sql`
  (filnamnets datum = körningsdatum, justeras vid faktisk körning) —
  `GRANT SELECT` → `mcp_readonly` (ingen PII → full SELECT OK, till skillnad från
  journalens fritextkolumner), `GRANT INSERT/DELETE/SELECT` → `etl_writer`.
  Följer T2/T3-mönstret.
- **Prod-ordning (dokumenteras, körs EJ i detta arbete):** admin `py db.py`
  (DDL, prod-schema är admin-initierat) → applicera grants-migration → ETL-reload
  (`load_saft.py --period … --override`). Lokal-först; prod är Didriks beslut.

---

## 8. Utanför scope (YAGNI)

- Ingen TaxInformation / CurrencyCode-ExchangeRate-detalj / SourceDocumentID m.fl.
  B3-fält — endast Analysis-dimensioner.
- Ingen SIE-implementation nu (schema är unified-ready; `sie_parser.py`
  `RE_TRANS`/`RE_PSALDO` + `load_sie.py` = dokumenterat följdsteg).
- Inga reporting-vyer / pivot-integration — rålager + MCP-semantikdoc räcker först.
- Ingen FK-tvång fakta→dim.
