# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Reset: restore all files for re-processing (move Referens/ back, delete output/)
py reset.py --dry-run                 # preview all periods
py reset.py --period 202604           # reset a specific period
py reset.py                           # reset all periods

# Run all countries in sequence
py run_all.py --dry-run               # preview all (previous month)
py run_all.py --period 202604         # run all for a specific period
py run_all.py                         # run all (previous month)

# Step 1: extract attachments from .msg emails → extracted/{period}/{Country}/
py extract.py                         # previous month (auto-detected)
py extract.py --period 202604         # specific period

# Step 1 (preview only):
py dry_run.py                         # previous month (auto-detected)
py dry_run.py --period 202604         # specific period

# Emails must be placed in: _inbox/{YYYYMM}/*.msg  (e.g. _inbox/202604/)

# Step 2: process by country (all scripts accept --period YYYYMM)
py process_norway.py --dry-run        # preview (previous month)
py process_norway.py                  # run
py process_norway.py --period 202604  # specific period
py process_norway.py --prefix 198     # single company

py process_sweden.py --dry-run
py process_sweden.py
py process_sweden.py --period 202604

py process_finland.py                 # all companies (previous month)
py process_finland.py 146             # single company
py process_finland.py 134 196         # multiple companies
py process_finland.py --dry-run       # preview
py process_finland.py --period 202604 # specific period

py process_denmark.py                 # all companies
py process_denmark.py 229 242         # specific companies
py process_denmark.py --dry-run
py process_denmark.py --period 202604

py process_germany.py                 # all companies
py process_germany.py 231 245         # specific companies
py process_germany.py --dry-run
py process_germany.py --period 202604

# Step 3: ladda till DuckDB (fact_balances). source_kind = 'IMP' (FI/DK/DE),
# 'SIE'+'SIE_PSALDO' (SE), 'SAFT' (NO). Konfliktkoll: skip om data redan finns
# för (bolag, period[, FY]); --override (global eller per bolag) skriver över
# och raderar senare-månader inom FY för SE/NO. Lager-isolering: rör aldrig
# MAN/IMP_ADJ.
py load_inl.py  --period 202604                       # FI/DK/DE Excel
py load_inl.py  --period 202604 --override            # global override
py load_inl.py  --period 202604 --override 134 196    # bara dessa bolag

py load_sie.py  --period 202604                       # SE (auto-period från #PSALDO, inkl. #VER/#TRANS)
py load_sie.py  --period 202604 --override 32         # rulla över bolag 32
py load_sie.py  --period 202604 --no-include-journal  # hoppa över journal (snabbare för stora filer)

py load_saft.py --period 202604                       # NO + DK (auto-period från header, inkl. GeneralLedgerEntries)
py load_saft.py --period 202604 --country DK          # bara Danmark (NO/DK; default = båda)
py load_saft.py --period 202604 --override
py load_saft.py --period 202604 --no-include-journal  # hoppa över journal

# Radera utfall — --source_kind alltid krav (lager-isolering).
# IMP på SE/NO → hela FY (SIE+SIE_PSALDO+journal eller SAFT+journal).
# IMP på FI/DK/DE → bara den månaden. IMP_ADJ/MAN → alltid bara den månaden.
py delete_db.py --period 202604 --source_kind IMP --dry-run
py delete_db.py --period 202604 --source_kind IMP --company 134 196
py delete_db.py --period 202604 --source_kind IMP --country Sweden
py delete_db.py --period 202604 --source_kind MAN --company 134
```

Use `py` (not `python`) on this Windows machine.

## Architecture

### Data flow

```
_inbox/{YYYYMM}/*.msg
    └─ extract.py ──────────────────────────────→ extracted/{period}/{Country}/{ID:03d}_{filename}
                                                        │
                              ┌─────────────────────────┤
                              ▼                         ▼
                    process_sweden.py           process_norway.py
                    process_finland.py          process_denmark.py
                    process_germany.py
                              │
                              ├── output/    ← INL.xlsx (FI/DK/DE) or renamed SIE/SAF-T (SE/NO)
                              └── Referens/  ← source files moved here after processing
```

All country scripts guard against reprocessing by checking whether source files have already been moved to `Referens/`.

### Central master data: `_params/Dotterbolagslista.xlsx` (gitignored)

Sheet `"Data For Company Find"`. Key columns (0-indexed):
- B (1) = BolagsID (integer, e.g. 32 → prefix `032`)
- C (2) = Market / Country (`Sweden`, `Norway`, `Finland`, `Denmark`, `Germany`)
- E (4) = Friendly name (used in output filenames)
- F (5) = OrgNr (used to verify/correct file prefixes in SE/NO)
- H (7) = Kind — rows with `"consolidated"` are always skipped
- J (9) = Domain (used for sender-domain matching in extract.py)

### extract.py / dry_run.py — company matching

Each `.msg` file is matched to a company via weighted scoring:
`filename=100, subject=80, attachment_name=60, sender=40, body=20`

- Full token match scores at full weight; partial match at 60% of weight.
- Sender domain match (col J) overrides to sender-weight score.
- `OVERRIDES` dict (msg stem → BolagsID) hard-codes ambiguous mails.
- `ATTACHMENT_OVERRIDES` (msg stem + attachment name substring → BolagsID) handles a single mail with attachments for multiple companies.

Score < 40 = low confidence (flagged `[LOW]`). Score 999 = manual override.

### Manuella filer: `_uploads/{period}/`

För källfiler som inte kan extraheras ur `.msg` (krypterad mail, mail som aldrig kom) finns `_uploads/{period}/` under base_path — på samma nivå som `_inbox/`, inte inuti det. `extract.py --period {period}` kör `process_uploads()` automatiskt efter msg-loopen.

- Filnamn måste börja med `{ID}_` (BolagsID från Dotterbolagslistan) — prefixet är ground truth, ingen scoring körs.
- Råkällor kopieras till `extracted/{period}/{Country}/`; färdiga `{ID}_{Namn}_{YYYYMM}_INL.xlsx` routas direkt till `{Country}/output/`.
- `_uploads` har prioritet: för varje bolag med en `_uploads`-fil arkiveras ev. msg-extraherade källor till `Referens/` först.

### Output filename conventions

| Country | Format |
|---------|--------|
| Sweden | `{ID:03d}_{FriendlyName}_SIE_{StartYear}-{EndYYYYMM}.SE` |
| Norway | `{ID:03d}_{FriendlyName}_{SoftwareAbbr}_SAF-T_{Year}-{Period}.xml` |
| Denmark / Finland / Germany | `{code}_{FriendlyName}_{YYYYMM}_INL.xlsx` |

INL.xlsx layout: empty row 1, then IS rows, then BS rows (col A=account, B=name, C=amount). Column C must sum to ~0.

### Country-script structure

- **Sweden** (`process_sweden.py`): parses SIE files (encoding fallback: utf-8-sig → cp437 → latin-1), resolves correct BolagsID via OrgNr lookup, validates `#RAR 0` for YTD period, renames, moves non-SIE files to `Referens/`.
- **Norway** (`process_norway.py`): handles both raw `.xml` and zipped `.zip` SAF-T; extracts from zip to renamed `.xml`. Uses `SOFTWARE_MAP` to abbreviate `SoftwareID` XML field.
- **Denmark** (`process_denmark.py`): `COMPANY_DEFS` dict configures each company's IS/BS account boundary (`is_max`, `bs_min` as 4-digit prefixes), filename, and extra files to move. Company 216 is IS-only (no BS). Company 178 skips bold+underline summary rows. Bolag 081 (Actas) levererar SAF-T XML och laddas av `load_saft.py` (inte INL.xlsx). Bolag 190 (Sikring Nord) hanteras här via XLSX; en eventuell SAF-T-export från E-Komplet saknar GL-konton och bara WARN:as i `load_saft.py`.
- **Finland** (`process_finland.py`): each company has its own `run_NNN()` function registered in `RUNNERS` dict. Multiple reader formats (A–L) handle the variety of Finnish accounting software exports (Fennoa CSV, Muutos CSV/XLSX, period XLS, etc.). BS accounts 1–1999 (4-digit prefix) have sign flipped. Accounts `237X` (årets resultat) are excluded to avoid double-counting.
- **Germany** (`process_germany.py`): `COMPANY_DEFS` dict with three readers: `monthly_value` (188 Bofferding — English-DATEV XLSX, negate all amounts), `susa_pro_monat` (231/245 — Haben−Soll at cols 8–9), `susa_jahresuebersicht` (246 — dynamic month column with S/H indicators), `susa_csv` (220 Weckbacher — cp1252 semicolon CSV, amount=−(Soll+Haben)). All exclude accounts ≥9000 (sub-ledger/statistical). Period detected dynamically via `prev_month_period()`.

### Paths

All scripts load the Dropbox root from `config.json` in the repo root (gitignored):
```json
{"base_path": "C:\\Users\\DidWac\\Prosero Dropbox\\...\\Get testfiles"}
```
Create this file before first run. `shared.load_config()` reads it and raises a clear error if missing.
`_params/` is relative to the repo root (`__file__`).

### Terminal output format

All process scripts emit structured log lines parseable by a future GUI:
```
[START]  process_denmark.py  period 202603  [DRY RUN]
[OK]     229  Sparad: 229_Zipp Systems_202603_INL.xlsx
[WARN]   134  Summa ≠ 0 (diff: 0.01)
[SKIP]   178  Filen saknas (redan i Referens?)
[INFO]   188  IS=72, BS=18  Summa=0.0000  OK
[ERROR]  220  Läsfel: Sheet 'IS' hittades inte
[DONE]   process_denmark.py  3 OK  1 WARN  1 SKIP  0 ERROR
```
Use `shared.log(status, label, msg)` for all status output. Status values: `START`, `OK`, `WARN`, `SKIP`, `INFO`, `ERROR`, `DONE`.

`shared.log_event(status, label, msg)` skriver bara till JSONL utan stdout-utskrift. Används av `dry_run.py` för att markera per-bolags-träffar (status `MATCH`) så att GUI:t kan visa "(✓)" i Extr-kolumnen för bolag som dry-run-matchats men inte extraherats än.

### Adding a new monthly period

All scripts accept `--period YYYYMM` and discover files dynamically — no code changes needed for routine months.

1. Place `.msg` files in `_inbox/{YYYYMM}/`.
2. Run `py extract.py --period YYYYMM` → creates `extracted/{YYYYMM}/{Country}/`.
3. Run `py run_all.py --period YYYYMM` (or individual process scripts with `--period`).

**If ambiguous mails appear** (extract.py flags them as LOW or wrong match): add entries to `OVERRIDES` / `ATTACHMENT_OVERRIDES` in `extract.py` and `dry_run.py`.

Period detection per script:
- **Sweden**: from SIE file `#RAR 0` line (YTD period dates).
- **Norway**: from SAF-T XML `<PeriodStart>` / `<PeriodEnd>` headers.
- **Finland / Denmark / Germany**: from `--period` arg (or `prev_month_period()` fallback). Files discovered via glob patterns in `COMPANY_DEFS` / `RUNNERS`.
