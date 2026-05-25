# RUNBOOK T8 — Flytta kod ur synkad Dropbox-mapp

**Status:** ✅ live (2026-05-25)
**SPEC:** `finance-warehouse_security_remediation_SPEC.md` §T8
**Owner:** [Claude Code] — repo-hygien, ingen Azure-/DB-påverkan

---

## Mål (per SPEC)

- Säkerställ att den kanoniska versionen av varje script finns i git-repot.
- Ersätt Dropbox-kopiorna med repo som single source of truth.
- Acceptanskriterier: ingen `.py` som skriver mot prod ligger kvar i den synkade
  mappen; diff mot repo = 0 (eller medveten merge gjord).

## Genomförda ändringar

### Diff-analys

Båda Dropbox-filerna är **pre-git-föregångare** (apr 24/28) till skript som nu
finns i repot (`extract.py` + `process_norway.py`, maj 22). Ingen "merge" möjlig —
repo-versionerna är vidareutvecklingar med:
- Strukturerad logging (`shared.log`)
- `--period` CLI-stöd
- `--dry-run` läge
- Hård scoring + override-tabeller (extract.py)
- Konsoliderad `Dotterbolagslista.xlsx`-läsning (`shared.load_companies`)
- Validering av output-radkonsistens

Repo-versionerna har körts produktivt sedan 2026-05-02.

### Flytt till arkiv

| Fil | Från | Till | SHA-256 |
|---|---|---|---|
| `extract_attachments.py` | `Dropbox\...\Get testfiles\` | `C:\Users\DidWac\dev\_archive\dropbox-finance-automation-2026-05-25\` | `a03471f9...` |
| `norway_saft.py` | `Dropbox\...\Get testfiles\` | `C:\Users\DidWac\dev\_archive\dropbox-finance-automation-2026-05-25\` | `316e0b0e...` |

Hash verifierad identisk pre- och post-flytt.

### Archive-location

`C:\Users\DidWac\dev\_archive\` är **systerträd till git-repot** — inte inuti git,
inte i Dropbox. Spårbarhet bevarad men:
- Ingen auto-sync (Cowork ser inte filerna)
- Ingen kod-yta i git-repot (cleanup-belastning)
- README.md i arkivet dokumenterar varför filerna finns där

## Acceptanskriterier — verifiering

```bash
DROP="C:/Users/DidWac/Prosero Dropbox/Didrik Wachtmeister/Phoenix Foundation/April alla filer/Get testfiles"

# Inga .py-filer kvar i Dropbox-roten
ls "$DROP"/*.py 2>&1
# Förväntat: "No such file or directory"
```

✅ verifierat 2026-05-25.

## Bevarad rest-risk

**Datamappar i Dropbox finns kvar** (`_inbox/`, `_uploads/`, `_history/`,
`_statistics/`, `extracted/`, `_skipped/`). SPEC kräver bara att **kod** flyttas
— datamappar är hela syftet med "Get testfiles". Cowork ska kunna se data men
inte exekvera skript.

`finance-warehouse_security_remediation_SPEC.md` (denna spec själv) ligger kvar i
Dropbox — det är **dokumentation, inte kod**, och rätt plats för delning med
Prosero IT/DPO.

## Beroenden / nästa steg

- **Inget.** T8 är isolerad från Azure/DB/secrets/roller.
- Om Dropbox-mappen behöver rensas helt (radera arkivet senare): se README i
  archive-mappen.

## Commit

```
chore: T8 — arkivera extract_attachments.py + norway_saft.py ur Dropbox
```
