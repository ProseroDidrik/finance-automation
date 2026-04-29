# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Step 1: extract attachments from .msg emails → extracted/{Country}/
py extract.py

# Step 1 (preview only):
py dry_run.py

# Step 2: process by country
py norway_saft.py --dry-run        # preview
py norway_saft.py                  # run
py norway_saft.py --prefix 198     # single company

py process_sweden.py --dry-run
py process_sweden.py

py process_finland.py              # all companies
py process_finland.py 146          # single company
py process_finland.py 134 196      # multiple companies

py process_denmark.py              # all companies
py process_denmark.py 229 242      # specific companies
py process_denmark.py --dry-run
```

Use `py` (not `python`) on this Windows machine.

## Architecture

### Data flow

```
_inbox/*.msg
    └─ extract.py ──────────────────────────────→ extracted/{Country}/{ID:03d}_{filename}
                                                        │
                              ┌─────────────────────────┤
                              ▼                         ▼
                    process_sweden.py           norway_saft.py
                    process_finland.py          process_denmark.py
                              │
                              ├── output/  ← INL.xlsx (FI/DK) or renamed SIE/SAF-T
                              └── Referens/ ← source files moved here after processing
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

### Output filename conventions

| Country | Format |
|---------|--------|
| Sweden | `{ID:03d}_{FriendlyName}_SIE_{StartYear}-{EndYYYYMM}.SE` |
| Norway | `{ID:03d}_{FriendlyName}_{SoftwareAbbr}_SAF-T_{Year}-{Period}.xml` |
| Denmark / Finland | `{code}_{FriendlyName}_{YYYYMM}_INL.xlsx` |

INL.xlsx layout: empty row 1, then IS rows, then BS rows (col A=account, B=name, C=amount). Column C must sum to ~0.

### Country-script structure

- **Sweden** (`process_sweden.py`): parses SIE files (encoding fallback: utf-8-sig → cp437 → latin-1), resolves correct BolagsID via OrgNr lookup, validates `#RAR 0` for YTD period, renames, moves non-SIE files to `Referens/`.
- **Norway** (`norway_saft.py`): handles both raw `.xml` and zipped `.zip` SAF-T; extracts from zip to renamed `.xml`. Uses `SOFTWARE_MAP` to abbreviate `SoftwareID` XML field.
- **Denmark** (`process_denmark.py`): `COMPANY_DEFS` dict configures each company's IS/BS account boundary (`is_max`, `bs_min` as 4-digit prefixes), filename, and extra files to move. Company 216 is IS-only (no BS). Company 178 skips bold+underline summary rows. Company 190 (Actas) is SAF-T-only, not INL.xlsx.
- **Finland** (`process_finland.py`): each company has its own `run_NNN()` function registered in `RUNNERS` dict. Multiple reader formats (A–L) handle the variety of Finnish accounting software exports (Fennoa CSV, Muutos CSV/XLSX, period XLS, etc.). BS accounts 1–1999 (4-digit prefix) have sign flipped. Accounts `237X` (årets resultat) are excluded to avoid double-counting.

### Paths

All scripts resolve source files relative to a hardcoded Dropbox path:
```
C:\Users\DidWac\Prosero Dropbox\Didrik Wachtmeister\Phoenix Foundation\April alla filer\Get testfiles
```
`_params/` is relative to the repo root (`__file__`).

### Adding a new monthly period

- **Finland**: update `period="YYYYMM"` and filenames in each `run_NNN()` function.
- **Denmark**: update `file` and `extra` filenames in `COMPANY_DEFS`.
- **Sweden / Norway**: scripts detect period dynamically from file content (SIE `#RAR 0` / SAF-T XML headers).
- **extract.py**: update `OVERRIDES` / `ATTACHMENT_OVERRIDES` for any new ambiguous mails.
