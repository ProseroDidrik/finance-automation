# SAF-T historik-dimensions-backfill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Backfilla `fact_saft_analysis`-dimensioner på den befintliga, redan korrekta historiska SAF-T-journalen (2022–2025) utan att röra journal/balans, B1ms-säkert via commit-per-period.

**Architecture:** Ny fristående `backfill_file_analysis()` i `load_saft.py` (rör INTE `load_file`) som återanvänder `parse_saft`/`iter_saft_journal`/`line_rows`/`dim_analysis_rows`. En pure helper `group_analysis_by_period()` gör den testbara kärnan (ValueDate-bunden gruppering). Wire:as in i `load_history_sie_saft.py` via `--analysis-only`. Varje period COPY:as+commit:as i egen transaktion → idempotent, återstartbar, bundet B1ms-tryck.

**Tech Stack:** Python 3 (stdlib `unittest`, `xml.etree` iterparse), PostgreSQL via psycopg (COPY). Kör med `py`, tester med `py -m unittest`. Spec: `docs/superpowers/specs/2026-05-29-saft-history-dimension-backfill-design.md`.

---

## Filstruktur

| Fil | Ansvar | Ändring |
|---|---|---|
| `load_saft.py` | `group_analysis_by_period` (pure) + `backfill_file_analysis` (DB, commit/period) | Modify |
| `tests/test_saft_backfill.py` | Pure-unit för `group_analysis_by_period` | Create |
| `load_history_sie_saft.py` | `--analysis-only`-flagga → kallar `backfill_file_analysis` för SAF-T | Modify |
| `scripts/verify_saft_backfill.py` | Manuell integrationsverifiering mot lokal Postgres | Create |
| `CLAUDE.md` | Dokumentera `--analysis-only` | Modify |

`load_file` (månadsladdaren) lämnas **orörd** — backfillen duplicerar de få dim-upsert-SQL-strängarna istället, för noll regressionsrisk.

---

## Task 1: Pure helper `group_analysis_by_period`

**Files:**
- Modify: `load_saft.py` (ny funktion efter `line_rows`)
- Test: `tests/test_saft_backfill.py` (Create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_saft_backfill.py`:

```python
"""Pure-unit för analys-grupperingen i load_saft.py (ingen DB).

group_analysis_by_period grupperar analystupler per ValueDate-härledd period via
line_rows — kärnan i historik-backfillen. Bekräftar att perioden ärvs från
ValueDate (b711832-skydd) och att cutoff/multi-block hanteras."""
import unittest
from datetime import date, datetime

import load_saft

NOW = datetime(2026, 5, 29)


def _line(value_date, transaction_date, analysis, debit=100.0):
    return {
        "journal_id": "J1", "journal_desc": "d",
        "transaction_id": "T1", "transaction_date": transaction_date,
        "transaction_desc": "td", "value_date": value_date,
        "line_no": 1, "record_id": "1", "account_code": "3000",
        "line_desc": "x", "debit": debit, "credit": 0.0,
        "analysis": analysis,
    }


class GroupAnalysisByPeriod(unittest.TestCase):
    def test_groups_by_value_date_period(self):
        lines = [
            _line(date(2024, 3, 15), date(2024, 1, 31), [("DEP", "3")]),
            _line(date(2024, 1, 5), date(2024, 1, 5), [("DEP", "1")]),
        ]
        out = load_saft.group_analysis_by_period(
            lines, company_id=9, currency="NOK", rel_src="x.xml",
            now=NOW, fallback_period="202412")
        self.assertEqual(set(out), {"202403", "202401"})
        self.assertEqual(out["202403"][0][6], "DEP")   # analysis_type
        self.assertEqual(out["202403"][0][1], "202403")  # period i tupeln

    def test_cutoff_excludes_later_period(self):
        lines = [_line(date(2025, 1, 10), date(2025, 1, 10), [("DEP", "1")])]
        out = load_saft.group_analysis_by_period(
            lines, 9, "NOK", "x.xml", NOW,
            fallback_period="202412", period_cutoff="202412")
        self.assertEqual(out, {})   # jp 202501 > cutoff 202412 → skippad

    def test_multi_block_grouped_under_same_period(self):
        lines = [_line(date(2024, 6, 2), date(2024, 6, 2),
                       [("DEP", "3"), ("PRO", "1")])]
        out = load_saft.group_analysis_by_period(
            lines, 9, "NOK", "x.xml", NOW, fallback_period="202412")
        self.assertEqual(len(out["202406"]), 2)
        self.assertEqual([t[6] for t in out["202406"]], ["DEP", "PRO"])

    def test_line_without_analysis_absent(self):
        lines = [_line(date(2024, 6, 2), date(2024, 6, 2), [])]
        out = load_saft.group_analysis_by_period(
            lines, 9, "NOK", "x.xml", NOW, fallback_period="202412")
        self.assertEqual(out, {})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -m unittest tests.test_saft_backfill -v`
Expected: FAIL — `AttributeError: module 'load_saft' has no attribute 'group_analysis_by_period'`.

- [ ] **Step 3: Write minimal implementation**

I `load_saft.py`, lägg till direkt efter funktionen `line_rows` (den slutar med
`return journal_tuple, analysis_tuples, jp, False`):

```python
def group_analysis_by_period(lines, company_id, currency, rel_src, now,
                             fallback_period, period_cutoff=None):
    """Gruppera analystupler per period ur journal-linjer (ingen DB).

    Återanvänder line_rows → varje analysrad ärver linjens ValueDate-period (jp).
    Returnerar dict[period -> list[analysis_tuple]]. Linjer med jp > period_cutoff
    skippas (samma cutoff som load_file). Kärnan i historik-backfillen, testbar
    utan databas."""
    by_period: dict[str, list[tuple]] = {}
    for line in lines:
        _jt, ats, jp, skipped = line_rows(
            line, company_id, currency, rel_src, now, fallback_period,
            period_cutoff=period_cutoff)
        if skipped or not ats:
            continue
        by_period.setdefault(jp, []).extend(ats)
    return by_period
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -m unittest tests.test_saft_backfill -v`
Expected: PASS (4 tester).

- [ ] **Step 5: Commit**

```bash
git add load_saft.py tests/test_saft_backfill.py
git commit -m "feat(saft): group_analysis_by_period — ValueDate-bunden gruppering (pure)"
```

---

## Task 2: `backfill_file_analysis` (commit per period)

**Files:**
- Modify: `load_saft.py` (ny funktion efter `group_analysis_by_period`)

Funktionen rör ALDRIG `fact_journal_saft`/`fact_balances`. Den härleder period +
cutoff exakt som `load_file` (så analysperioderna matchar den redan laddade
journalen), upsertar dim, och COPY:ar analys **en period per transaktion**.

- [ ] **Step 1: Skriv implementationen**

I `load_saft.py`, efter `group_analysis_by_period`:

```python
def backfill_file_analysis(con, path, base_path, period_override, orgnr_lookup,
                           *, dry_run=False):
    """Backfilla BARA fact_saft_analysis för en (historisk) SAF-T-fil.

    Rör ALDRIG fact_journal_saft/fact_balances — journalen är redan laddad och
    korrekt periodiserad. Commit per period (B1ms-säkert, idempotent,
    återstartbar). period/cutoff härleds som i load_file → analysperioder == den
    befintliga journalens perioder.
    """
    try:
        parsed = parse_saft(path)
    except Exception as e:
        log("ERROR", path.name, f"Läsfel: {e}")
        return "error"

    country = parsed.get("country")
    if country not in NS_BY_COUNTRY:
        log("ERROR", path.name, f"Okänd SAF-T-namespace ({parsed.get('ns')!r})")
        return "error"

    # Företag (samma logik som load_file)
    orgnr_raw = parsed.get("orgnr")
    company_id = None
    if not orgnr_raw:
        for substr, cid in FILENAME_OVERRIDES.items():
            if substr in path.name:
                company_id = cid
                break
        if company_id is None:
            log("ERROR", path.name, "Saknar Header/Company/RegistrationNumber")
            return "error"
    else:
        hit = orgnr_lookup.get(normalize_orgnr(orgnr_raw))
        if not hit:
            log("ERROR", path.name, f"OrgNr {orgnr_raw} saknas i dim_company")
            return "error"
        company_id, _name = hit

    period = derive_period(parsed, period_override)
    if not period:
        log("ERROR", company_id, f"Kunde inte härleda period från {path.name}")
        return "error"
    currency = parsed.get("currency") or DEFAULT_CURRENCY[country]
    rel_src = db.relpath_from_base(path, base_path)
    now = datetime.now()

    # En journal-iter → analys grupperad per period (ValueDate-bunden).
    by_period = group_analysis_by_period(
        iter_saft_journal(path, parsed["ns"]),
        company_id, currency, rel_src, now,
        fallback_period=period, period_cutoff=period_override)
    type_rows, member_rows = dim_analysis_rows(
        parsed.get("analysis_types", []), company_id, now)
    total = sum(len(v) for v in by_period.values())

    if dry_run:
        log("OK", company_id,
            f"[DRY] {path.name}  analys={total} i {len(by_period)} perioder "
            f"dim_type={len(type_rows)} dim_member={len(member_rows)} (journal orörd)")
        return "ok"

    # Dim-upsert (egen liten transaktion). SQL dupliceras medvetet från load_file
    # för att hålla load_file orört (noll regressionsrisk på månadsladdaren).
    con.execute("BEGIN")
    try:
        if type_rows:
            con.executemany(
                """INSERT INTO dim_analysis_type
                   (company_id, source_format, analysis_type, description, loaded_at)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (company_id, source_format, analysis_type)
                   DO UPDATE SET description = EXCLUDED.description,
                                 loaded_at = EXCLUDED.loaded_at""",
                type_rows)
        if member_rows:
            con.executemany(
                """INSERT INTO dim_analysis_member
                   (company_id, source_format, analysis_type, analysis_id,
                    description, loaded_at)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (company_id, source_format, analysis_type, analysis_id)
                   DO UPDATE SET description = EXCLUDED.description,
                                 loaded_at = EXCLUDED.loaded_at""",
                member_rows)
        con.execute("COMMIT")
    except Exception as e:
        con.execute("ROLLBACK")
        log("ERROR", company_id, f"dim-upsert-fel {path.name}: {e}")
        return "error"

    # Analys: en transaktion PER period (B1ms-säkert + idempotent + återstartbar).
    loaded = 0
    for p in sorted(by_period):
        rows = by_period[p]
        con.execute("BEGIN")
        try:
            con.execute(
                "DELETE FROM fact_saft_analysis WHERE company_id = %s AND period = %s",
                [company_id, p])
            cur = con.cursor()
            try:
                with cur.copy(_COPY_ANALYSIS_SAFT) as cp:
                    for row in rows:
                        cp.write_row(row)
            finally:
                cur.close()
            db.sync_dim_period(con, [p])
            con.execute("COMMIT")
            loaded += len(rows)
        except Exception as e:
            con.execute("ROLLBACK")
            log("ERROR", company_id, f"analys-fel {path.name} period {p}: {e}")
            return "error"

    log("OK", company_id,
        f"{path.name}  analys={loaded} i {len(by_period)} perioder (journal orörd)")
    return "ok"
```

- [ ] **Step 2: Verifiera att modulen importerar rent + enhetstester gröna**

Run: `py -c "import load_saft; print('import OK')"`
Expected: `import OK`

Run: `py -m unittest tests.test_saft_backfill tests.test_load_saft_analysis -v`
Expected: PASS (alla).

- [ ] **Step 3: Commit**

```bash
git add load_saft.py
git commit -m "feat(saft): backfill_file_analysis — analys-only, commit per period (journal orörd)"
```

---

## Task 3: Wire `--analysis-only` i `load_history_sie_saft.py`

**Files:**
- Modify: `load_history_sie_saft.py` (`load_year` SAF-T-gren ~rad 168-182; `main` flagga ~rad 196-204; anrop ~rad 242-248; `load_year`-signatur ~rad 122-126)

- [ ] **Step 1: Lägg `--analysis-only`-flaggan i main()**

I `load_history_sie_saft.py` `main()`, efter `--include-journal`-argumentet (~rad 196-197):

```python
    parser.add_argument("--analysis-only", action="store_true",
                        help="Backfilla BARA fact_saft_analysis för SAF-T-filer "
                             "(rör inte journal/balans). SIE-filer hoppas över.")
```

- [ ] **Step 2: Skicka flaggan genom till load_year**

I `main()`, i `load_year(...)`-anropet (~rad 242-248), lägg argumentet:

```python
            counts = load_year(
                con, year, year_dir, base_path,
                orgnr_lookup_sie, orgnr_lookup_saft,
                dry_run=args.dry_run, include_journal=args.include_journal,
                allowed_formats=allowed_formats,
                override=args.override,
                analysis_only=args.analysis_only,
            )
```

- [ ] **Step 3: Ta emot + agera på flaggan i load_year**

I `load_year`-signaturen (~rad 122-126), lägg `analysis_only: bool = False` som
keyword-arg (efter `override`):

```python
def load_year(con: db.Conn, year: int, year_dir: Path,
              base_path: Path, orgnr_lookup_sie: dict, orgnr_lookup_saft: dict,
              *, dry_run: bool, include_journal: bool,
              allowed_formats: set[str],
              override: list[int] | None = None,
              analysis_only: bool = False) -> dict[str, int]:
```

I SIE-grenen (~rad 149), lägg en tidig skip när analysis_only (SIE har inga
dimensioner att backfilla) — direkt efter `if fmt == "sie":`:

```python
        if fmt == "sie":
            if analysis_only:
                log("SKIP", path.name, "analysis-only: SIE har inga SAF-T-dimensioner")
                counts["skip"] += 1
                continue
```

I SAF-T-grenen, ersätt anropet (~rad 176-182) så att analysis_only routar till
backfill:

```python
            if analysis_only:
                status = load_saft.backfill_file_analysis(
                    con, path, base_path, period_fallback,
                    orgnr_lookup_saft, dry_run=dry_run)
            else:
                status = load_saft.load_file(
                    con, path, base_path, period_fallback,
                    orgnr_lookup_saft,
                    dry_run=dry_run,
                    include_journal=include_journal,
                    override=override,
                )
```

- [ ] **Step 4: Verifiera flagga + import (DB-fritt)**

`load_history.main()` ansluter via `db.connect(role='etl')`, vars rollvakt
(`_enforce_non_admin`) kan blockera mot den lokala dev-superusern — så vi
DB-testar inte den vägen här (det riktiga backfill-beteendet täcks av Task 4 som
ansluter med `role='admin'`, och prod-körningen går via etl_writer där vakten
passerar).

Run: `py -c "import load_history_sie_saft; print('import OK')"`
Expected: `import OK`

Run: `py load_history_sie_saft.py --help`
Expected: hjälptexten listar `--analysis-only`.

- [ ] **Step 5: Commit**

```bash
git add load_history_sie_saft.py
git commit -m "feat(history): --analysis-only routar SAF-T till backfill_file_analysis"
```

---

## Task 4: Manuell integrationsverifiering (lokal Postgres)

**Files:**
- Create: `scripts/verify_saft_backfill.py`

Bevisar den kritiska invarianten: backfillen rör INTE journal/balans, och är idempotent.

- [ ] **Step 1: Skriv scriptet**

Create `scripts/verify_saft_backfill.py`:

```python
"""Manuell integrationsverifiering av historik-backfillen mot lokal Postgres.
RÖR ALDRIG PROD — kräver localhost-DATABASE_URL.

Bevisar: backfill_file_analysis fyller fact_saft_analysis MEN lämnar
fact_journal_saft + fact_balances oförändrade, och är idempotent.

    docker start finance-pg-dev
    $env:DATABASE_URL = "postgresql://dev:dev@localhost:5432/finance"
    py scripts/verify_saft_backfill.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402

if "localhost" not in os.environ.get("DATABASE_URL", ""):
    sys.exit("VÄGRAR: DATABASE_URL pekar inte på localhost — kör bara mot lokal dev.")


def _counts(con, cid):
    j = con.execute("SELECT COUNT(*) FROM fact_journal_saft WHERE company_id=%s",
                    [cid]).fetchone()[0]
    b = con.execute("SELECT COUNT(*) FROM fact_balances "
                    "WHERE company_id=%s AND source_kind='SAFT'", [cid]).fetchone()[0]
    a = con.execute("SELECT COUNT(*) FROM fact_saft_analysis WHERE company_id=%s",
                    [cid]).fetchone()[0]
    return j, b, a


def main():
    from shared import load_config
    from load_saft import backfill_file_analysis, build_orgnr_lookup
    base = Path(load_config()["base_path"])
    # Liten historisk NO-fil 2024 (undvik Actas). Välj första icke-Actas xml.
    cand = sorted(p for p in (base / "_history" / "2024").glob("*.xml")
                  if "Actas" not in p.name)
    if not cand:
        sys.exit("Hittade ingen historisk NO-xml i _history/2024")
    path = cand[0]
    con = db.connect(role="admin")
    try:
        db.init_schema(con)
        lookup = build_orgnr_lookup(con)
        # Kör en gång (full load_saft.load_file behöver INTE ha körts först —
        # vi mäter bara att journal/balans inte ÄNDRAS av backfillen).
        from load_saft import parse_saft, normalize_orgnr
        parsed = parse_saft(path)
        cid = lookup.get(normalize_orgnr(parsed["orgnr"]))[0]
        j0, b0, a0 = _counts(con, cid)
        backfill_file_analysis(con, path, base, "202412", lookup, dry_run=False)
        j1, b1, a1 = _counts(con, cid)
        print(f"[{'OK' if j1==j0 and b1==b0 else 'FAIL'}] journal/balans orörda: "
              f"journal {j0}->{j1}, balans {b0}->{b1}")
        print(f"[{'OK' if a1>0 else 'FAIL'}] analys fylld: {a0}->{a1}")
        # Idempotens: kör igen → analys-radantal stabilt
        backfill_file_analysis(con, path, base, "202412", lookup, dry_run=False)
        j2, b2, a2 = _counts(con, cid)
        print(f"[{'OK' if a2==a1 else 'FAIL'}] idempotent: analys {a1}->{a2}")
        print(f"[{'OK' if j2==j0 and b2==b0 else 'FAIL'}] journal/balans fortf. orörda")
    finally:
        con.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Kör verifieringen**

Run: `DATABASE_URL="postgresql://dev:dev@localhost:5432/finance" py scripts/verify_saft_backfill.py`
Expected: alla rader `[OK]` — journal/balans oförändrade, analys fylld, idempotent.

- [ ] **Step 3: Commit**

```bash
git add scripts/verify_saft_backfill.py
git commit -m "chore(scripts): integrationsverifiering av historik-backfill (journal orörd)"
```

---

## Task 5: Dokumentation

**Files:**
- Modify: `CLAUDE.md` (databasinläsnings-sektionen — load-kommandona)

- [ ] **Step 1: Lägg --analysis-only-exempel**

I `CLAUDE.md`, i blocket med `load_saft.py`-kommandon (efter `--force`-raden, ~rad 71), lägg:

```bash
# Backfilla BARA dimensioner på historisk SAF-T (rör inte journal/balans):
py load_history_sie_saft.py --format saft --analysis-only --years 2022 2023 2024 2025
py load_history_sie_saft.py --format saft --analysis-only --years 2024 --dry-run
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude): --analysis-only historik-backfill-kommando"
```

---

## Slutverifiering

- [ ] Hela enhetssviten grön: `py -m unittest discover -s tests -p "test_*.py"`
- [ ] (Lokal DB) `py scripts/verify_saft_backfill.py` alla `[OK]`.
- [ ] Spec §-täckning (self-review nedan).

## Utrullning (prod, attended, EJ del av kodarbetet)

Körs när B1ms `state=Ready` + responsiv:
1. Verifiera B1ms Ready.
2. `DATABASE_URL_ETL=<kv> py load_history_sie_saft.py --format saft --analysis-only --years 2022 2023 2024 2025` — övervaka per år (icke-Actas går snabbt; Actas commit:ar per period så trycket är bundet).
3. Verifiera per år: `fact_saft_analysis` växer, `fact_journal_saft`/`fact_balances` intakta, 202604 orört.
4. B1ms tippar (osannolikt med per-period-commit) → stop+start via `az rest` (se memory), kör om — backfillen är idempotent per period.
