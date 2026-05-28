# SAF-T Dimensioner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persistera SAF-T `Analysis`-dimensioner (kostnadsställe/avdelning/projekt) i en fristående, månads-stämplad analys-faktatabell + delade dim-namntabeller, frågbara per månad/YTD/FY/LTM.

**Architecture:** `parse_saft` läser `AnalysisTypeTable` (namn); `iter_saft_journal` yieldar `analysis`-block per linje. En ny pure helper `line_rows()` bygger journal- OCH analys-tupler ur EN ValueDate-härledd period (`_journal_period`) → omöjligt att periodisera analysen annorlunda än journalen (skyddar mot b711832-regression). `load_saft.py` laddar dim-tabeller (upsert) + `fact_saft_analysis` via ett andra COPY-pass, med idempotens-paritet mot journalens DELETE:ar.

**Tech Stack:** Python 3 (stdlib `xml.etree.ElementTree` iterparse, `unittest`), PostgreSQL via psycopg (COPY). Kör med `py` (rot-`.venv`), tester med `py -m unittest`. Spec: `docs/superpowers/specs/2026-05-28-saft-dimensions-design.md`.

---

## Filstruktur

| Fil | Ansvar | Ändring |
|---|---|---|
| `saft_parser.py` | Kanonisk parse: lägg `analysis_types` i `parse_saft`, `analysis`-nyckel i `iter_saft_journal` | Modify |
| `db.py` | `SCHEMA_SQL`: ny sekvens + 3 tabeller | Modify |
| `load_saft.py` | Pure helpers `dim_analysis_rows`/`line_rows` + wiring (dim-upsert, 2:a COPY, DELETE-paritet) | Modify |
| `tests/test_saft_parser.py` | Parser-kontrakt: AnalysisTypeTable + analysis-yield | Modify |
| `tests/test_load_saft_analysis.py` | Pure-unit: `line_rows` (period-bindning, multi-block, cutoff), `dim_analysis_rows` (dedup) | Create |
| `scripts/saft_regression_oracle.py` | Utöka fingerprint med analys; regenerera golden | Modify |
| `tests/saft_oracle_golden.json` | Omfångat golden (medveten `--capture`) | Modify (regen) |
| `db/migrations/20260528_analysis_dimension_tables.sql` | Grants till mcp_readonly + etl_writer | Create |
| `docs/warehouse_semantics.md` | Femte fälla + periodsemantik-not | Modify |
| `scripts/verify_saft_analysis.py` | Manuell integrationsverifiering mot lokal Postgres | Create |

---

## Task 1: `parse_saft` läser AnalysisTypeTable

**Files:**
- Modify: `saft_parser.py` (funktionen `parse_saft`, ~rad 91-152)
- Test: `tests/test_saft_parser.py`

- [ ] **Step 1: Write the failing test**

Lägg till i `tests/test_saft_parser.py` (följ filens befintliga inline-XML-mönster — den skriver XML till temp-fil och anropar `saft_parser.parse_saft`):

```python
class AnalysisTypeTable(unittest.TestCase):
    """parse_saft ska läsa MasterFiles/AnalysisTypeTable → out['analysis_types']."""

    NS = "urn:StandardAuditFile-Taxation-Financial:NO"

    def _write(self, body: str) -> Path:
        xml = (f'<AuditFile xmlns="{self.NS}"><Header>'
               f'<Company><RegistrationNumber>916059701</RegistrationNumber>'
               f'<Name>X</Name></Company></Header>'
               f'<MasterFiles>{body}</MasterFiles></AuditFile>')
        f = Path(tempfile.mkstemp(suffix=".xml")[1])
        f.write_text(xml, encoding="utf-8")
        return f

    def test_reads_type_and_member_descriptions(self):
        f = self._write(
            '<AnalysisTypeTable>'
            '<AnalysisTypeTableEntry><AnalysisType>DEP</AnalysisType>'
            '<AnalysisTypeDescription>Avdeling</AnalysisTypeDescription>'
            '<AnalysisID>3</AnalysisID>'
            '<AnalysisIDDescription>Montørstab</AnalysisIDDescription>'
            '<Status>Active</Status></AnalysisTypeTableEntry>'
            '</AnalysisTypeTable>')
        try:
            parsed = saft_parser.parse_saft(f)
        finally:
            f.unlink()
        self.assertEqual(parsed["analysis_types"],
                         [("DEP", "Avdeling", "3", "Montørstab")])

    def test_missing_table_yields_empty_list(self):
        f = self._write("")  # ingen AnalysisTypeTable (NO 1.20 / DK 1.0)
        try:
            parsed = saft_parser.parse_saft(f)
        finally:
            f.unlink()
        self.assertEqual(parsed["analysis_types"], [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -m unittest tests.test_saft_parser.AnalysisTypeTable -v`
Expected: FAIL — `KeyError: 'analysis_types'`.

- [ ] **Step 3: Write minimal implementation**

I `saft_parser.py`, `parse_saft`: lägg `"analysis_types": []` i `out`-dicten (efter `"accounts": []`, ~rad 109), initiera `analysis_types: list[tuple] = []` bredvid `accounts` (~rad 111), lägg en gren i iterparse-loopen (före `GeneralLedgerEntries`-grenen, ~rad 145), och sätt `out["analysis_types"] = analysis_types` före `return` (~rad 151):

```python
        elif tag == "AnalysisTypeTableEntry":
            analysis_types.append((
                _t(elem, "AnalysisType", ns),
                _t(elem, "AnalysisTypeDescription", ns),
                _t(elem, "AnalysisID", ns),
                _t(elem, "AnalysisIDDescription", ns),
            ))
            elem.clear()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -m unittest tests.test_saft_parser.AnalysisTypeTable -v`
Expected: PASS (2 tester).

- [ ] **Step 5: Commit**

```bash
git add saft_parser.py tests/test_saft_parser.py
git commit -m "feat(saft): parse_saft läser AnalysisTypeTable (dimensionsnamn)"
```

---

## Task 2: `iter_saft_journal` yieldar `analysis` per linje

**Files:**
- Modify: `saft_parser.py` (funktionen `iter_saft_journal`, ~rad 166-224)
- Test: `tests/test_saft_parser.py`

- [ ] **Step 1: Write the failing test**

Lägg till i `tests/test_saft_parser.py` (filen har redan ett mönster för att skriva journal-XML; återanvänd dess hjälpare om sådan finns, annars inline enligt nedan):

```python
class JournalAnalysis(unittest.TestCase):
    NS = "urn:StandardAuditFile-Taxation-Financial:NO"

    def _write_journal(self, line_inner: str) -> Path:
        xml = (f'<AuditFile xmlns="{self.NS}"><GeneralLedgerEntries><Journal>'
               f'<JournalID>J1</JournalID><Description>d</Description>'
               f'<Transaction><TransactionID>T1</TransactionID>'
               f'<TransactionDate>2026-04-30</TransactionDate>'
               f'<Line><RecordID>1</RecordID><AccountID>3000</AccountID>'
               f'{line_inner}'
               f'<DebitAmount><Amount>100</Amount></DebitAmount></Line>'
               f'</Transaction></Journal></GeneralLedgerEntries></AuditFile>')
        f = Path(tempfile.mkstemp(suffix=".xml")[1])
        f.write_text(xml, encoding="utf-8")
        return f

    def test_two_analysis_blocks_yielded(self):
        f = self._write_journal(
            '<Analysis><AnalysisType>DEP</AnalysisType><AnalysisID>3</AnalysisID></Analysis>'
            '<Analysis><AnalysisType>PRO</AnalysisType><AnalysisID>1</AnalysisID></Analysis>')
        try:
            rows = list(saft_parser.iter_saft_journal(f, self.NS))
        finally:
            f.unlink()
        self.assertEqual(rows[0]["analysis"], [("DEP", "3"), ("PRO", "1")])

    def test_no_analysis_blocks_yields_empty(self):
        f = self._write_journal("")
        try:
            rows = list(saft_parser.iter_saft_journal(f, self.NS))
        finally:
            f.unlink()
        self.assertEqual(rows[0]["analysis"], [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -m unittest tests.test_saft_parser.JournalAnalysis -v`
Expected: FAIL — `KeyError: 'analysis'`.

- [ ] **Step 3: Write minimal implementation**

I `saft_parser.py`, `iter_saft_journal`, inuti `for line in tx.findall(...)`-loopen (~rad 202-211), efter att `credit` beräknats och före `yield`: bygg analys-listan och lägg in i yield-dicten:

```python
                    analysis = [
                        (_t(a, "AnalysisType", ns), _t(a, "AnalysisID", ns))
                        for a in line.findall(f"{{{ns}}}Analysis")
                    ]
```

Lägg sedan `"analysis": analysis,` som ny nyckel i yield-dicten (~rad 212-220).

- [ ] **Step 4: Run test to verify it passes**

Run: `py -m unittest tests.test_saft_parser.JournalAnalysis -v`
Expected: PASS (2 tester).

- [ ] **Step 5: Commit**

```bash
git add saft_parser.py tests/test_saft_parser.py
git commit -m "feat(saft): iter_saft_journal yieldar analysis-block per linje"
```

---

## Task 3: Bevisa journal-kontrakt oförändrat + utöka orakelet med analys

Orakelet (`scripts/saft_regression_oracle.py`) hashar bara de 12 uppräknade journalfälten — INTE hela yield-dicten. Att lägga till `analysis` ändrar alltså inte `journal_sha256`. Vi (a) verifierar att de 12 fälten + accounts är byte-identiska (bevis: additivt, ingen regression), sedan (b) utökar orakelet att även fingerprinta analysen och regenererar golden medvetet.

**Files:**
- Modify: `scripts/saft_regression_oracle.py` (funktionen `fingerprint`, ~rad 63-102)
- Modify: `tests/saft_oracle_golden.json` (regenereras)

- [ ] **Step 1: Bevisa att journal+accounts-kontraktet är oförändrat**

Run: `py scripts/saft_regression_oracle.py --verify --slow`
Expected: `OK — fingerprint matchar golden`. (Bevisar att Task 1+2 är rent additiva — inget av de 12 journalfälten eller accounts-hashen rörts.)

> Om MISMATCH: STOPP. Task 1/2 har av misstag ändrat ett befintligt fält. Åtgärda innan du går vidare.

- [ ] **Step 2: Utöka fingerprint med analys**

I `scripts/saft_regression_oracle.py`, `fingerprint()`: lägg analys-hashning i journal-loopen (~rad 84-92, inuti `for j in ...`) och två nycklar i retur-dicten (~rad 94-102):

```python
    # i journal-loopen, efter jour.update(...):
        for atype, aid in j["analysis"]:
            ana.update((_h(j["transaction_id"], j["line_no"], atype, aid) + "\n").encode("utf-8"))
            n_analysis += 1
```

Initiera `ana = hashlib.sha256()` och `n_analysis = 0` bredvid `jour`/`n_journal` (~rad 82-83), och lägg i retur-dicten:

```python
        "n_analysis": n_analysis,
        "analysis_sha256": ana.hexdigest(),
```

Lägg även `"n_analysis"` och `"analysis_sha256"` i fält-tuplen i `verify()` (~rad 160-161) så de jämförs.

- [ ] **Step 3: Regenerera golden medvetet och diffa**

Run:
```bash
py scripts/saft_regression_oracle.py --capture --slow
git diff tests/saft_oracle_golden.json
```
Expected: diffen visar ENBART tillagda `n_analysis` + `analysis_sha256` per fil — inga ändrade `journal_sha256`/`accounts_sha256`/`n_journal`. (Det är beviset att omfånget är additivt.)

> Om någon befintlig hash ändrats i diffen: STOPP och utred.

- [ ] **Step 4: Verifiera att utökat orakel passerar**

Run: `py scripts/saft_regression_oracle.py --verify --slow`
Expected: `OK — fingerprint matchar golden`.

- [ ] **Step 5: Commit**

```bash
git add scripts/saft_regression_oracle.py tests/saft_oracle_golden.json
git commit -m "test(saft): omfånga regressions-orakel med analys-fingerprint (medveten regen)"
```

---

## Task 4: DB-schema — sekvens + 3 tabeller

**Files:**
- Modify: `db.py` (`SCHEMA_SQL`, sekvenser ~rad 241-248 och tabeller ~rad 335-359)

- [ ] **Step 1: Lägg sekvens**

I `db.py` `SCHEMA_SQL`, bland `CREATE SEQUENCE`-raderna (~rad 244):

```sql
CREATE SEQUENCE IF NOT EXISTS seq_fact_saft_analysis START 1;
```

- [ ] **Step 2: Lägg de tre tabellerna**

I `db.py` `SCHEMA_SQL`, efter `fact_journal_saft`-indexen (~rad 359, före `dim_account_map`):

```sql
CREATE TABLE IF NOT EXISTS dim_analysis_type (
    company_id      INTEGER NOT NULL,
    source_format   TEXT NOT NULL,         -- 'SAFT' | 'SIE'
    analysis_type   TEXT NOT NULL,
    description     TEXT,
    loaded_at       TIMESTAMP NOT NULL,
    PRIMARY KEY (company_id, source_format, analysis_type)
);

CREATE TABLE IF NOT EXISTS dim_analysis_member (
    company_id      INTEGER NOT NULL,
    source_format   TEXT NOT NULL,
    analysis_type   TEXT NOT NULL,
    analysis_id     TEXT NOT NULL,
    description     TEXT,
    loaded_at       TIMESTAMP NOT NULL,
    PRIMARY KEY (company_id, source_format, analysis_type, analysis_id)
);

CREATE TABLE IF NOT EXISTS fact_saft_analysis (
    id              BIGINT PRIMARY KEY DEFAULT nextval('seq_fact_saft_analysis'),
    company_id      INTEGER NOT NULL,
    period          TEXT NOT NULL,         -- ValueDate-härledd per linje (= journalens period)
    transaction_id  TEXT,
    line_no         INTEGER NOT NULL,
    record_id       TEXT,
    account_code    TEXT NOT NULL,
    analysis_type   TEXT NOT NULL,
    analysis_id     TEXT NOT NULL,
    amount          DOUBLE PRECISION NOT NULL,  -- linjens belopp (debit-credit), MÅNADSRÖRELSE
    currency        TEXT NOT NULL,
    source_file     TEXT NOT NULL,
    loaded_at       TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fsa_company_period ON fact_saft_analysis(company_id, period);
CREATE INDEX IF NOT EXISTS idx_fsa_type_member    ON fact_saft_analysis(company_id, analysis_type, analysis_id);
CREATE INDEX IF NOT EXISTS idx_fsa_account        ON fact_saft_analysis(account_code);
CREATE INDEX IF NOT EXISTS idx_fsa_period         ON fact_saft_analysis(period);
```

- [ ] **Step 3: Verifiera att schemat är syntaktiskt giltigt mot lokal Postgres**

Förutsättning: lokal container igång (`docker compose start postgres`) och
`$env:DATABASE_URL = "postgresql://dev:dev@localhost:5432/finance"`.

Run: `py -c "import os, db; con=db.connect(role='admin'); db.init_schema(con); print('schema OK'); con.close()"`
Expected: `schema OK` (idempotent — tabellerna skapas, ingen krasch).

- [ ] **Step 4: Commit**

```bash
git add db.py
git commit -m "feat(db): dim_analysis_type/_member + fact_saft_analysis i SCHEMA_SQL"
```

---

## Task 5: Pure helpers — `dim_analysis_rows` + `line_rows` (period-bindning)

Detta är korrekthetskärnan. `line_rows` härleder perioden EN gång via `_journal_period` och ger BÅDE journaltupeln och analystuplerna samma `jp` → analysen kan aldrig periodiseras annorlunda än journalen.

**Files:**
- Modify: `load_saft.py` (nya funktioner + import av `_journal_period` finns redan, ~rad 41-54)
- Test: `tests/test_load_saft_analysis.py` (Create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_load_saft_analysis.py`:

```python
"""Pure-unit-tester för analys-radbyggarna i load_saft.py (ingen DB).

Speglar mönstret i test_load_sie.py (psaldo_fact_rows). Den kritiska invarianten:
analysradens period = journalradens period = ValueDate per linje (skydd mot
b711832-regression i dimensionslagret).
"""
import unittest
from datetime import date, datetime

import load_saft

NOW = datetime(2026, 5, 28)


def _line(value_date, transaction_date, analysis):
    return {
        "journal_id": "J1", "journal_desc": "d",
        "transaction_id": "T1", "transaction_date": transaction_date,
        "transaction_desc": "td", "value_date": value_date,
        "line_no": 1, "record_id": "1", "account_code": "3000",
        "line_desc": "x", "debit": 100.0, "credit": 0.0,
        "analysis": analysis,
    }


class LineRowsPeriodBinding(unittest.TestCase):
    def test_period_from_value_date_not_transaction_date(self):
        # Tripletex-divergens: bokförd i jan, ValueDate i mars.
        line = _line(date(2026, 3, 15), date(2026, 1, 31), [("DEP", "3")])
        jt, ats, jp, skipped = load_saft.line_rows(
            line, company_id=9, currency="NOK", rel_src="x.xml",
            now=NOW, fallback_period="202604")
        self.assertEqual(jp, "202603")
        self.assertEqual(jt[1], "202603")           # journaltupelns period
        self.assertEqual(ats[0][1], "202603")       # analystupelns period == samma

    def test_fallback_to_transaction_date_when_no_value_date(self):
        line = _line(None, date(2026, 1, 31), [("DEP", "3")])
        jt, ats, jp, skipped = load_saft.line_rows(
            line, 9, "NOK", "x.xml", NOW, "202604")
        self.assertEqual(jp, "202601")
        self.assertEqual(ats[0][1], "202601")

    def test_multi_block_explosion_same_amount_same_period(self):
        line = _line(date(2026, 4, 2), date(2026, 4, 2), [("DEP", "3"), ("PRO", "1")])
        jt, ats, jp, skipped = load_saft.line_rows(
            line, 9, "NOK", "x.xml", NOW, "202604")
        self.assertEqual(len(ats), 2)
        self.assertEqual([a[6] for a in ats], ["DEP", "PRO"])     # analysis_type
        self.assertEqual([a[8] for a in ats], [100.0, 100.0])     # amount = debit-credit
        self.assertTrue(all(a[1] == "202604" for a in ats))

    def test_cutoff_skips_line_and_analysis(self):
        line = _line(date(2026, 5, 10), date(2026, 5, 10), [("DEP", "3")])
        jt, ats, jp, skipped = load_saft.line_rows(
            line, 9, "NOK", "x.xml", NOW, "202604", period_cutoff="202604")
        self.assertTrue(skipped)
        self.assertIsNone(jt)
        self.assertEqual(ats, [])


class DimAnalysisRows(unittest.TestCase):
    def test_dedup_types_and_members(self):
        analysis_types = [
            ("DEP", "Avdeling", "1", "Adm"),
            ("DEP", "Avdeling", "2", "Salg"),
            ("PRO", "Prosjekt", "1", "P1"),
        ]
        type_rows, member_rows = load_saft.dim_analysis_rows(
            analysis_types, company_id=9, now=NOW)
        self.assertEqual({r[2] for r in type_rows}, {"DEP", "PRO"})
        self.assertEqual(len(type_rows), 2)         # DEP deduplicerad
        self.assertEqual(len(member_rows), 3)
        # member-tupel: (company_id, source_format, analysis_type, analysis_id, desc, now)
        self.assertIn((9, "SAFT", "DEP", "1", "Adm", NOW), member_rows)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -m unittest tests.test_load_saft_analysis -v`
Expected: FAIL — `AttributeError: module 'load_saft' has no attribute 'line_rows'`.

- [ ] **Step 3: Write minimal implementation**

I `load_saft.py`, lägg till efter `_COPY_JOURNAL_SAFT` (~rad 340) det nya COPY-statementet och de två pure helpers. `_journal_period` är redan importerad (rad 43):

```python
_COPY_ANALYSIS_SAFT = """
COPY fact_saft_analysis
(company_id, period, transaction_id, line_no, record_id, account_code,
 analysis_type, analysis_id, amount, currency, source_file, loaded_at)
FROM STDIN
"""


def dim_analysis_rows(analysis_types, company_id, now, source_format="SAFT"):
    """(analysis_type, type_desc, analysis_id, id_desc)-lista (från parse_saft) →
    (type_rows, member_rows), deduplicerade. Ingen DB."""
    types: dict = {}
    members: dict = {}
    for atype, tdesc, aid, idesc in analysis_types:
        if atype is None:
            continue
        types[atype] = (company_id, source_format, atype, tdesc, now)
        if aid is not None:
            members[(atype, aid)] = (company_id, source_format, atype, aid, idesc, now)
    return list(types.values()), list(members.values())


def line_rows(line, company_id, currency, rel_src, now, fallback_period,
              period_cutoff=None):
    """Bygg (journal_tuple, analysis_tuples, jp, skipped) för EN journal-linje.

    jp härleds EN gång via _journal_period (ValueDate per linje → TransactionDate-
    fallback). BÅDE journaltupeln och alla analystupler får samma jp → analysen
    kan inte periodiseras annorlunda än journalen (skydd mot b711832-regression).
    period_cutoff: om satt och jp > cutoff → skipped=True (journal + analys droppas).
    """
    jp = _journal_period(line, fallback_period)
    if period_cutoff is not None and jp > period_cutoff:
        return None, [], jp, True
    debit = line["debit"] or 0.0
    credit = line["credit"] or 0.0
    amount = debit - credit
    journal_tuple = (
        company_id, jp,
        line["journal_id"], line["journal_desc"],
        line["transaction_id"], line["transaction_date"], line["transaction_desc"],
        line["line_no"], line["record_id"], line["account_code"],
        debit, credit, amount, line["line_desc"],
        currency, rel_src, now,
    )
    analysis_tuples = [
        (company_id, jp, line["transaction_id"], line["line_no"], line["record_id"],
         line["account_code"], atype, aid, amount, currency, rel_src, now)
        for (atype, aid) in line.get("analysis", [])
    ]
    return journal_tuple, analysis_tuples, jp, False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -m unittest tests.test_load_saft_analysis -v`
Expected: PASS (5 tester).

- [ ] **Step 5: Commit**

```bash
git add load_saft.py tests/test_load_saft_analysis.py
git commit -m "feat(saft): pure helpers line_rows + dim_analysis_rows (ValueDate-bunden analysperiod)"
```

---

## Task 6: Wiring i `load_file` — dim-upsert, 2:a COPY, DELETE-paritet

**Files:**
- Modify: `load_saft.py` (funktionen `load_file`, journal-sektionen ~rad 247-301; override-DELETE ~rad 220-231)

- [ ] **Step 1: Dim-upsert efter period-DELETE**

I `load_saft.py` `load_file`, inuti `con.execute("BEGIN")`-blocket, efter `fact_balances`-inserten (~rad 245) och före journal-sektionen: upserta dim-tabellerna ur `parsed["analysis_types"]`:

```python
        type_rows, member_rows = dim_analysis_rows(
            parsed.get("analysis_types", []), company_id, now)
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
```

- [ ] **Step 2: Spegla override-DELETE för fact_saft_analysis**

I override-grenen (~rad 220-231), efter `DELETE FROM fact_journal_saft ... period BETWEEN`-satsen, lägg den FY-breda analys-DELETE:n:

```python
            con.execute(
                """DELETE FROM fact_saft_analysis
                   WHERE company_id = %s AND period BETWEEN %s AND %s""",
                [company_id, fy_start, fy_end],
            )
```

- [ ] **Step 3: Använd `line_rows` i journal-pass-2 + 2:a COPY för analys**

Ersätt pass-2-loopen (~rad 277-299, `cur = con.cursor()` … `cur.close()`). Pass 1 (period-discovery, ~rad 261-266) lämnas orört. Efter att `journal_periods` är känt och journal-DELETE körts (~rad 267-273), lägg den speglade per-period analys-DELETE:n med SAMMA set:

```python
            if journal_periods:
                placeholders = ",".join(["%s"] * len(journal_periods))
                con.execute(
                    f"""DELETE FROM fact_saft_analysis
                        WHERE company_id = %s AND period IN ({placeholders})""",
                    [company_id, *sorted(journal_periods)],
                )
            # Pass 2: journal via COPY; analys buffras och COPY:as separat efteråt
            # (psycopg tillåter en COPY i taget per anslutning).
            analysis_buf: list[tuple] = []
            cur = con.cursor()
            try:
                with cur.copy(_COPY_JOURNAL_SAFT) as cp:
                    for j in iter_saft_journal(path, parsed["ns"]):
                        if j.get("value_date") is None:
                            journal_vdate_fallback += 1
                        jt, ats, jp, skipped = line_rows(
                            j, company_id, currency, rel_src, now, period,
                            period_cutoff=period_override)
                        if skipped:
                            journal_skipped += 1
                            continue
                        cp.write_row(jt)
                        analysis_buf.extend(ats)
                        journal_rows_loaded += 1
            finally:
                cur.close()
            analysis_rows_loaded = 0
            if analysis_buf:
                cur2 = con.cursor()
                try:
                    with cur2.copy(_COPY_ANALYSIS_SAFT) as cp:
                        for row in analysis_buf:
                            cp.write_row(row)
                            analysis_rows_loaded += 1
                finally:
                    cur2.close()
```

Lägg `analysis_rows_loaded = 0` bland init-variablerna (~rad 256-259) så den finns även när `include_journal=False`, och utöka `load_history`-meddelandet (~rad 311-313) med `analysis_rows={analysis_rows_loaded}`.

- [ ] **Step 4: Verifiera att enhetstester fortfarande passerar (ingen DB)**

Run: `py -m unittest discover -s tests -p "test_*.py"`
Expected: OK (alla befintliga + nya pure-unit-tester; DB-beroende integ hoppas/saknas).

- [ ] **Step 5: Commit**

```bash
git add load_saft.py
git commit -m "feat(saft): ladda fact_saft_analysis + dim-upsert med idempotens-paritet"
```

---

## Task 7: Grants-migration

**Files:**
- Create: `db/migrations/20260528_analysis_dimension_tables.sql`

- [ ] **Step 1: Skriv migrationen**

Create `db/migrations/20260528_analysis_dimension_tables.sql` (idempotent; följer T2/T3-mönstret — ingen PII i dessa tabeller, full SELECT till mcp_readonly):

```sql
-- T-dimensions: grants för analys-dimensionstabellerna.
-- Tabellerna skapas i db.py SCHEMA_SQL; denna migration sätter bara rättigheter.
-- mcp_readonly: SELECT (ingen PII). etl_writer: SELECT/INSERT/DELETE (DML, ingen DDL).

GRANT SELECT ON dim_analysis_type, dim_analysis_member, fact_saft_analysis
    TO mcp_readonly;

GRANT SELECT, INSERT, DELETE ON dim_analysis_type, dim_analysis_member,
    fact_saft_analysis TO etl_writer;

GRANT USAGE, SELECT ON SEQUENCE seq_fact_saft_analysis TO etl_writer;
```

- [ ] **Step 2: Applicera mot lokal Postgres och verifiera**

Förutsättning: lokala roller `mcp_readonly`/`etl_writer` finns (annars hoppa lokalt — de finns i prod). Mot lokal dev kan rollerna saknas; verifiera då bara att filen är giltig SQL genom att köra Task 4 Step 3 igen (schemat) — grants körs i prod enligt §7-utrullning.

Run (om roller finns lokalt): `py db\migrations\_apply.py db\migrations\20260528_analysis_dimension_tables.sql`
Expected: körs utan fel.

- [ ] **Step 3: Commit**

```bash
git add db/migrations/20260528_analysis_dimension_tables.sql
git commit -m "chore(db): grants för analys-dimensionstabeller (mcp_readonly + etl_writer)"
```

---

## Task 8: Warehouse-semantik — femte fälla + periodsemantik

**Files:**
- Modify: `docs/warehouse_semantics.md`

- [ ] **Step 1: Lägg till semantik-sektion**

Lägg till i `docs/warehouse_semantics.md` (intill de fyra befintliga fällorna — läs filen först för att matcha rubrik-/tonstil):

```markdown
### Fälla 5: fact_saft_analysis är en per-(linje,axel)-explosion

`fact_saft_analysis` lagrar SAF-T-dimensioner: en rad per journallinje × Analysis-
block, med linjens belopp upprepat per axel.

- **SUM:a ALDRIG över `analysis_type`.** En DK-linje kan ha upp till 9 axlar
  (VoTp + OrgUnit1..12) och hela beloppet upprepas per axel. Filtrera alltid på
  EN `analysis_type` (`WHERE analysis_type = 'DEP'`).
- **Odimensionerad rest:** täckning < 100% (~98% i NO; vissa linjer otaggade).
  `SUM(amount) WHERE analysis_type = X` ≤ journaltotal — resten är otaggad. Inga
  placeholder-rader finns.
- **amount är MÅNADSRÖRELSE (linjenivå), aldrig YTD.** Period sätts från ValueDate
  per linje (samma som fact_journal_saft). SUM:a aldrig fact_saft_analysis.amount
  mot fact_balances YTD (SE/NO) — det ger nonsens. Använd period-range för YTD/FY/LTM.

Mönster — avdelningsfördelad personalkostnad YTD för bolag 9:
\`\`\`sql
SELECT analysis_id, SUM(amount) AS belopp
FROM fact_saft_analysis
WHERE company_id = 9 AND analysis_type = 'DEP'
  AND period BETWEEN '202601' AND '202604'
GROUP BY analysis_id ORDER BY belopp;
\`\`\`
```

- [ ] **Step 2: Commit**

```bash
git add docs/warehouse_semantics.md
git commit -m "docs(warehouse): femte fälla — fact_saft_analysis per-axel-explosion + månadssemantik"
```

---

## Task 9: Integrationsverifiering mot lokal Postgres (manuell)

Den lokala ETL-rollkollen + beroendet av riktiga filer gör detta miljöspecifikt → ett manuellt script, inte ett blockerande unittest. Pure-unit-testerna (Task 5) täcker korrekthetskärnan; detta bevisar end-to-end mot riktig data.

**Files:**
- Create: `scripts/verify_saft_analysis.py`

- [ ] **Step 1: Skriv verifieringsscriptet**

Create `scripts/verify_saft_analysis.py`:

```python
"""Manuell integrationsverifiering: ladda NO 009 + DK 081 till lokal Postgres
och kontrollera analys-lagret. RÖR ALDRIG PROD — kräver localhost-DATABASE_URL.

    docker compose start postgres
    $env:DATABASE_URL = "postgresql://dev:dev@localhost:5432/finance"
    py scripts/verify_saft_analysis.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db
from saft_parser import iter_saft_journal, parse_saft, _journal_period

if "localhost" not in os.environ.get("DATABASE_URL", ""):
    sys.exit("VÄGRAR: DATABASE_URL pekar inte på localhost — kör bara mot lokal dev.")


def period_dist(path, fallback):
    """Periodfördelning för journal vs analys ur SAMMA iter (förväntas identisk)."""
    from collections import Counter
    jd, ad = Counter(), Counter()
    ns = parse_saft(path)["ns"]
    for j in iter_saft_journal(path, ns):
        jp = _journal_period(j, fallback)
        jd[jp] += 1
        for _ in j["analysis"]:
            ad[jp] += 1
    return jd, ad


def main():
    from shared import load_config
    from load_saft import build_orgnr_lookup, load_file
    base = Path(load_config()["base_path"])
    no = next((base / "extracted/202604/Norway").glob("009_*.xml"))
    con = db.connect(role="admin")
    try:
        db.init_schema(con)
        lookup = build_orgnr_lookup(con)
        load_file(con, no, base, "202604", lookup, dry_run=False,
                  include_journal=True, override=[], force=False)
        n = con.execute("SELECT COUNT(*) FROM fact_saft_analysis").fetchone()[0]
        types = con.execute("SELECT COUNT(*) FROM dim_analysis_type").fetchone()[0]
        print(f"[OK] fact_saft_analysis={n} rader, dim_analysis_type={types}")
        # Idempotens: ladda om med override → radantal oförändrat (inga dubbletter)
        load_file(con, no, base, "202604", lookup, dry_run=False,
                  include_journal=True, override=[9999999], force=False)
        n2 = con.execute("SELECT COUNT(*) FROM fact_saft_analysis").fetchone()[0]
        print(f"[{'OK' if n2 == n else 'FAIL'}] idempotens: {n} -> {n2}")
        # Period-bindning: journal- och analys-fördelningen ska ha samma periodnycklar
        jd, ad = period_dist(no, "202604")
        ok = set(jd) == set(ad)
        print(f"[{'OK' if ok else 'FAIL'}] period-bindning (samma nycklar journal/analys): {sorted(jd)}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Kör verifieringen (lokal Postgres)**

Förutsättning: `docker compose start postgres`, `$env:DATABASE_URL = "postgresql://dev:dev@localhost:5432/finance"`, `config.json` pekar på base_path med riktiga filer.

Run: `py scripts/verify_saft_analysis.py`
Expected: `[OK] fact_saft_analysis=… rader`, `[OK] idempotens: N → N`, `[OK] periodnycklar …`.

> Valfri Tripletex-fördjupning: byt 009 mot ett NO-bolag där ValueDate ≠ TransactionDate (158 Asker / 189) och bekräfta att analysens periodfördelning matchar journalens — den pure-unit-versionen i Task 5 är dock den blockerande regressionsvakten.

- [ ] **Step 3: Commit**

```bash
git add scripts/verify_saft_analysis.py
git commit -m "chore(scripts): manuell integrationsverifiering av analys-lagret mot lokal Postgres"
```

---

## Slutverifiering

- [ ] Hela enhetssviten grön utan DB: `py -m unittest discover -s tests -p "test_*.py"`
- [ ] Orakel grönt: `py scripts/saft_regression_oracle.py --verify --slow`
- [ ] (Manuellt, lokal DB) `py scripts/verify_saft_analysis.py` grönt.
- [ ] Designspecens §1-§8 har var sin task (se self-review nedan).

## Prod-utrullning (EJ del av detta arbete — Didriks beslut)

1. Admin DDL: `py db.py` (skapar tabeller; prod-schema är admin-initierat).
2. Grants: applicera `db/migrations/20260528_analysis_dimension_tables.sql` mot prod.
3. ETL-reload: `py load_saft.py --period 202604 --override` (fyller fact_saft_analysis för alla SAF-T-bolag). OBS B1ms-strypning vid tung DK 081-reload (se memory).
