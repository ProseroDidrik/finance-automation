# Design: Backfill SAF-T-dimensioner på historik (2022–2025)

**Datum:** 2026-05-29
**Gren:** `feat/saft-history-dimensions` (off `origin/main`, efter PR #27)
**Status:** Godkänd design — redo för implementationsplan.
**Bygger på:** `docs/superpowers/specs/2026-05-28-saft-dimensions-design.md` (dimensionslagret).

## Bakgrund & mål

`fact_saft_analysis` (SAF-T-dimensioner) finns sedan PR #27, men bara för period
**202604**. Den historiska SAF-T-journalen (2022–2025) saknar dimensioner.

Verifierat mot prod innan detta spår:
- **Journal-historik finns redan** (SAF-T 2022=735k … 2025=1.0M rader/år) — premissen
  "historiken saknar journal" var fel.
- **Periodiseringen är redan korrekt** — Tripletex-bolagen 158/189 har 2024-journal
  jämnt spridd över alla 12 månader (inte klumpad i jan). Alltså laddades historiken
  med ValueDate-fixen; en omladdning fixar inga felaktiga siffror.
- Källfiler finns i `_history/{2022..2025}/` (~135 SAF-T-filer; Actas/081 årsvis är
  446–512MB).

**Slutsats:** den enda luckan är dimensioner. Arbetet är **rent additivt** — vi ska
lägga `fact_saft_analysis` på den befintliga, korrekta journalen **utan att röra
journal/balans**.

SIE-historik är utanför scope (SIE-dimensioner är inte byggda än — eget framtida
spår; då laddas SIE-historik om en gång och får dimensioner då).

## Arkitektur

Ny **fristående funktion** `backfill_file_analysis(con, path, ...)` i `load_saft.py`.
Den rör INTE `load_file` (månadsladdaren) → ingen regressionsrisk. Återanvänder
`parse_saft` (`analysis_types`), `iter_saft_journal` (`analysis`-nyckel) och
`line_rows` (ValueDate-bunden period — samma `_journal_period`, så analysen ärver
historikens redan korrekta periodisering).

Wire:as in i `load_history_sie_saft.py` via flaggan `--analysis-only`: när satt
(och format=saft) anropas `backfill_file_analysis` per upptäckt SAF-T-fil istället
för full `load_file`.

## Flöde (per fil) — `backfill_file_analysis`

1. `parse_saft(path)` → upserta `dim_analysis_type` + `dim_analysis_member`
   (`ON CONFLICT`, egen liten transaktion). Tål att `AnalysisTypeTable` saknas.
2. **En** journal-iter → bygg `analysis_by_period: dict[str, list[tuple]]` genom att
   gruppera `line_rows(...)`-analystupler på `jp`. `period_cutoff` appliceras som i
   `load_file` (skippa `jp > cutoff`). Buffras klient-sidan (~1.5M tupler för Actas
   = några hundra MB RAM, acceptabelt).
3. **Per period** (sorterad) — egen transaktion:
   `BEGIN; DELETE FROM fact_saft_analysis WHERE company_id=%s AND period=%s;`
   COPY periodens tupler via `_COPY_ANALYSIS_SAFT`; `COMMIT`.
   → bundar B1ms-trycket (~250k rader/commit för Actas), **idempotent** (delete+insert
   per period) och **återstartbar mitt i en fil**.
4. `db.sync_dim_period` för de berörda perioderna.

**Ingen balans-conflict-skip** (till skillnad från `load_file`): backfillen ska
alltid refresh:a analysen för filens perioder, oavsett att balans/journal redan finns.
**Rör aldrig** `fact_balances` eller `fact_journal_saft`.

Företagsmatchning: samma som `load_history` (orgnr ur fil → prod `dim_company`;
Actas via `FILENAME_OVERRIDES`).

## Scope

- **År 2022–2025, alla SAF-T-bolag** med filer i `_history/` (~135 filer, **inkl.
  Actas** — ofarligt tack vare commit-per-period).
- Hoppar SIE och 202604 (har redan analys).

## Utanför scope (YAGNI)

- **Skräpår-städning** (`fact_journal_saft` med år `0001/2002/2004/2006`, ~1540 rader
  från trasiga ValueDates): separat journal-datakvalitets-följdspår (godkänt "senare").
  Backfillen speglar journalens periodisering konsekvent; dessa rader är försumbara.
- **SIE-dimensioner** (eget framtida spår).
- Ändringar i `load_file` (månadsladdaren) — orörd.

## Tester

- **Pure-unit (ingen DB):** grupperings-/cutoff-logiken — en funktion som tar en
  lista journal-linjer (dicts) + period_override och returnerar `analysis_by_period`.
  Synthetiska linjer (ValueDate≠TransactionDate, cutoff, multi-block). Återanvänder
  `line_rows`. Bekräftar att grupperingen ärver ValueDate-perioden.
- **Integration (lokal Postgres, RÖR ALDRIG PROD):** backfill en liten historisk
  NO-fil →
  - `fact_saft_analysis` fylls för filens perioder,
  - **KRITISKT: `fact_journal_saft`- och `fact_balances`-radantal är OFÖRÄNDRADE**
    före/efter (bevisar att journalen inte rörs),
  - idempotent omkörning → stabilt analys-radantal (per-period delete+insert),
  - per-period-commit syns (flera commits för en flermånaders fil).

## Utrullning (prod, attended, när B1ms = "Ready")

1. Verifiera B1ms `state=Ready` + connection-responsivitet.
2. `py load_history_sie_saft.py --format saft --analysis-only --years 2022 2023 2024 2025`
   — icke-Actas-bolag, övervakat per år.
3. Actas (081) per år, ett i taget, övervakat (commit-per-period gör det säkert).
4. Verifiera per år: analys-radantal rimligt, `fact_journal_saft`/`fact_balances`
   intakta, 202604 orört.

CLI-detaljer (load_history-flaggor som ska finnas/återanvändas): `--format saft`,
`--analysis-only` (ny), `--years`, valfri bolagslista. `--include-journal` är
irrelevant i analysis-only-läget (journal rörs inte).
