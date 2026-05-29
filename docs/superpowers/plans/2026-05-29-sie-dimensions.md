# SIE-dimensioner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persistera SIE:s `#DIM`/`#OBJEKT`-dimensioner i warehouse via en ny `fact_sie_analysis`-tabell som speglar den live SAF-T-modellen.

**Architecture:** `sie_parser.py` fångar `#DIM`/`#OBJEKT`-deklarationer och `#TRANS`-objektlistor (idag bortkastade). `load_sie.py` upsertar axel-/medlemsnamn till de generiska `dim_analysis_type`/`dim_analysis_member` (`source_format='SIE'`) och skriver en analys-rad per (`#TRANS`-linje × dim-par) till nya `fact_sie_analysis`. Period = verifikatets månad, ärvd i samma loop som journalraderna → paritet med `fact_journal_sie`.

**Tech Stack:** Python 3.14, `unittest`, psycopg3, PostgreSQL. Windows: `py`. Tester körs med `py -m unittest`.

---

## Filstruktur

| Fil | Ansvar | Åtgärd |
|---|---|---|
| `db.py` | `SCHEMA_SQL`: ny `fact_sie_analysis` + `seq_fact_sie_analysis` | Modify (~rad 406, efter SAF-T-analys-indexen) |
| `sie_parser.py` | Parsa `#DIM`/`#OBJEKT` + `#TRANS`-objektlista; `parse_object_list` | Modify |
| `load_sie.py` | `sie_dim_analysis_rows`-builder; analys-rader ur `vouchers_to_journal_rows`; upsert + insert + DELETE-paritet | Modify |
| `tests/test_sie_parser.py` | Enhet: dim-deklarationer + `analysis`-extraktion | Modify |
| `tests/test_load_sie.py` | Enhet: `sie_dim_analysis_rows` + period-bindning (speglar `test_load_saft_analysis.py`) | Modify |
| `db/migrations/20260529_sie_analysis_grants.sql` | Grants för `fact_sie_analysis` | Create |
| `scripts/verify_sie_analysis.py` | Manuell lokal-Postgres-integration (speglar `verify_saft_analysis.py`) | Create |
| `docs/warehouse_semantics.md` | Mental model för SIE-analys (femte fälla) | Modify |
| `SCHEMA.md` | Dokumentera `fact_sie_analysis` | Modify |

**Not om regressions-orakel:** SIE har inget golden-orakel (bara SAF-T har `saft_oracle_golden.json`). Regressionsvakten för parser-kontraktsändringen är de befintliga `TransDimensions`-testerna (brace-no-leak + voucher-balans) — de MÅSTE förbli gröna genom Task 2.

**Liten medveten avvikelse från spec §3:** vid defekt objektlista (udda token-antal) droppar den rena parsern det dinglande token tyst (ingen WARN). WARN bedöms low-value (beloppet hamnar ändå rätt; defekta objektlistor är extremt sällsynta). "Skip" behålls; "WARN" utgår (YAGNI).

---

## Task 1: Schema — fact_sie_analysis + sekvens

**Files:**
- Modify: `db.py` (SCHEMA_SQL, efter `idx_fsa_period`-raden ~406)

- [ ] **Step 1: Lägg till tabell, sekvens och index i SCHEMA_SQL**

Lägg in EFTER `CREATE INDEX ... idx_fsa_period ON fact_saft_analysis(period);` (db.py ~rad 406):

```sql
CREATE SEQUENCE IF NOT EXISTS seq_fact_sie_analysis START 1;

-- SIE-dimensioner: en rad per (#TRANS-linje × dim-par). period = verifikatets
-- månad (= fact_journal_sie). amount = #TRANS-beloppet, MÅNADSRÖRELSE.
-- Multi-dim upprepar beloppet → SUM aldrig över analysis_type. Delar
-- dim_analysis_type/_member med SAF-T via source_format='SIE'.
CREATE TABLE IF NOT EXISTS fact_sie_analysis (
    id              BIGINT PRIMARY KEY DEFAULT nextval('seq_fact_sie_analysis'),
    company_id      INTEGER NOT NULL,
    period          TEXT NOT NULL,
    series          TEXT,
    voucher_number  TEXT,
    line_no         INTEGER NOT NULL,
    account_code    TEXT NOT NULL,
    analysis_type   TEXT NOT NULL,
    analysis_id     TEXT NOT NULL,
    amount          DOUBLE PRECISION NOT NULL,
    currency        TEXT NOT NULL,
    source_file     TEXT NOT NULL,
    loaded_at       TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fsie_company_period ON fact_sie_analysis(company_id, period);
CREATE INDEX IF NOT EXISTS idx_fsie_type_member    ON fact_sie_analysis(company_id, analysis_type, analysis_id);
CREATE INDEX IF NOT EXISTS idx_fsie_account        ON fact_sie_analysis(account_code);
CREATE INDEX IF NOT EXISTS idx_fsie_period         ON fact_sie_analysis(period);
```

- [ ] **Step 2: Verifiera att SCHEMA_SQL är välformad**

Run: `py -c "import db; assert 'fact_sie_analysis' in db.SCHEMA_SQL; assert 'seq_fact_sie_analysis' in db.SCHEMA_SQL; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add db.py
git commit -m "feat(sie-dims): fact_sie_analysis-tabell + sekvens i SCHEMA_SQL"
```

---

## Task 2: Parser — #DIM/#OBJEKT + #TRANS-objektlista

**Files:**
- Modify: `sie_parser.py` (regexar ~rad 23, `parse_sie` rad 89-197)
- Test: `tests/test_sie_parser.py`

- [ ] **Step 1: Skriv de fallerande testerna**

Lägg till i `tests/test_sie_parser.py` (i klassen `TransDimensions` eller en ny klass `DimDeclarations`):

```python
class DimDeclarations(unittest.TestCase):
    def test_dim_and_objekt_parsed(self):
        p = sie_parser.parse_sie(
            '#DIM 1 "Avdelning"\n#DIM 6 "Projekt"\n'
            '#OBJEKT 1 "100" "Administration"\n'
            '#OBJEKT 6 "9000300" "Projekt X"\n')
        self.assertIn(("1", "Avdelning"), p["dims"])
        self.assertIn(("6", "Projekt"), p["dims"])
        self.assertIn(("1", "100", "Administration"), p["objekt"])
        self.assertIn(("6", "9000300", "Projekt X"), p["objekt"])

    def test_dim_objekt_absent_is_empty(self):
        p = sie_parser.parse_sie('#ORGNR 556071-2340\n')
        self.assertEqual(p["dims"], [])
        self.assertEqual(p["objekt"], [])

    def test_objekt_unquoted_objektnr(self):
        p = sie_parser.parse_sie('#OBJEKT 1 100 "Adm"\n')
        self.assertIn(("1", "100", "Adm"), p["objekt"])


class TransAnalysis(unittest.TestCase):
    def _transes(self, ver_text):
        return sie_parser.parse_sie(ver_text, with_journal=True)["vouchers"][0]["transes"]

    def test_multidim_pairs_extracted(self):
        t = self._transes(
            '#VER "IN26" 1 20260131 "x"\n{\n'
            '\t#TRANS 7830 {"1" "100" "6" "9000300"} 1247.27 20260131 "Avskr" 1\n'
            '\t#TRANS 1209 {} -1247.27\n}\n')
        self.assertEqual(t[0]["analysis"], [("1", "100"), ("6", "9000300")])
        self.assertEqual(t[0]["amount"], 1247.27)   # belopp oförändrat
        self.assertEqual(t[0]["quantity"], 1.0)     # quantity oförändrad
        self.assertEqual(t[1]["analysis"], [])      # tom brace → tom lista

    def test_unquoted_dim_tokens(self):
        t = self._transes('#VER A 1 20260101 "x"\n{\n#TRANS 5420 {1 2} 333.7\n}\n')
        self.assertEqual(t[0]["analysis"], [("1", "2")])

    def test_odd_token_count_drops_dangling(self):
        t = self._transes('#VER A 1 20260101 "x"\n{\n#TRANS 5420 {1 "100" 2} 333.7\n}\n')
        self.assertEqual(t[0]["analysis"], [("1", "100")])
        self.assertEqual(t[0]["amount"], 333.7)
```

- [ ] **Step 2: Kör testerna — verifiera att de fallerar**

Run: `py -m unittest tests.test_sie_parser.DimDeclarations tests.test_sie_parser.TransAnalysis -v`
Expected: FAIL (`KeyError: 'dims'` resp. `KeyError: 'analysis'`)

- [ ] **Step 3: Lägg till regexar och tokenizer i sie_parser.py**

Efter `RE_KONTO` (rad 23) lägg till:

```python
RE_DIM    = re.compile(r'^#DIM\s+(\S+)\s+"([^"]*)"', re.IGNORECASE)
RE_OBJEKT = re.compile(r'^#OBJEKT\s+(\S+)\s+"?([^"\s]+)"?\s+"([^"]*)"', re.IGNORECASE)
# Token i en objektlista: citerat ("100") eller ociterat (100).
RE_OBJ_TOKEN = re.compile(r'"([^"]*)"|(\S+)')
```

Ändra `RE_TRANS` (rad 57-63) till **namngivna grupper** (skyddar mot gruppförskjutning när brace-innehållet fångas):

```python
RE_TRANS  = re.compile(
    r'^#TRANS\s+(?P<account>\S+)\s+\{(?P<dims>[^}]*)\}\s+'
    r'(?P<amount>-?\d+(?:[.,]\d+)?)'          # belopp
    r'(?:\s+(?P<transdat>\d{8}))?'            # transdat (ociterat YYYYMMDD)
    r'(?:\s+"(?P<text>[^"]*)")?'              # text
    r'(?:\s+(?P<quantity>-?\d+(?:[.,]\d+)?))?',  # quantity
    re.IGNORECASE,
)
```

Lägg till hjälpfunktion (t.ex. efter `normalize_orgnr`):

```python
def parse_object_list(braces: str) -> list[tuple[str, str]]:
    """#TRANS-objektlistans innehåll → lista av (dim, objekt)-par.

    Tokens kan vara citerade ("1") eller ociterade (1) och kommer i par
    (dimensionsnr, objektnr). Ett dinglande udda token (defekt lista) droppas
    tyst — beloppet ligger utanför braces och påverkas aldrig.
    """
    toks = [a if a else b for a, b in RE_OBJ_TOKEN.findall(braces)]
    return [(toks[i], toks[i + 1]) for i in range(0, len(toks) - 1, 2)]
```

- [ ] **Step 4: Initiera nya nycklar + parsa #TRANS-analys + top-level #DIM/#OBJEKT**

I `parse_sie` `out`-dicten (rad 98-104) lägg till `"dims": [], "objekt": [],`.

I `in_block`-grenen där `RE_TRANS` matchas (rad 126-144), byt grupp-index mot namn och lägg till `analysis`:

```python
        if in_block:
            if with_journal and current_voucher is not None and (m := RE_TRANS.match(line)):
                try:
                    amt = float(m.group("amount").replace(",", "."))
                except ValueError:
                    continue
                line_no_in_voucher += 1
                quantity = None
                if m.group("quantity"):
                    try:
                        quantity = float(m.group("quantity").replace(",", "."))
                    except ValueError:
                        quantity = None
                current_voucher["transes"].append({
                    "line_no": line_no_in_voucher,
                    "account": m.group("account"),
                    "amount": amt,
                    "trans_text": m.group("text"),
                    "quantity": quantity,
                    "analysis": parse_object_list(m.group("dims")),
                })
            continue
```

I top-level-grenen, efter `RE_KONTO`-elif (rad 154-155), lägg till:

```python
        elif m := RE_DIM.match(line):
            out["dims"].append((m.group(1), m.group(2)))
        elif m := RE_OBJEKT.match(line):
            out["objekt"].append((m.group(1), m.group(2), m.group(3)))
```

- [ ] **Step 5: Kör de nya + de befintliga regressionstesterna**

Run: `py -m unittest tests.test_sie_parser -v`
Expected: PASS — inkl. `test_multidim_brace_does_not_leak_into_amount`, `test_voucher_with_dims_still_balances`, `test_tab_separated_visma_net` (regressionsvakten för gruppförskjutningen).

- [ ] **Step 6: Commit**

```bash
git add sie_parser.py tests/test_sie_parser.py
git commit -m "feat(sie-dims): parsa #DIM/#OBJEKT + #TRANS-objektlista (namngivna grupper)"
```

---

## Task 3: Loader — sie_dim_analysis_rows-builder (ren unit)

**Files:**
- Modify: `load_sie.py` (ny funktion nära `psaldo_fact_rows`, rad ~308)
- Test: `tests/test_load_sie.py`

- [ ] **Step 1: Skriv det fallerande testet**

Lägg till i `tests/test_load_sie.py`:

```python
from datetime import datetime as _dt

class SieDimAnalysisRows(unittest.TestCase):
    NOW = _dt(2026, 5, 29)

    def test_types_and_members_from_two_lists(self):
        dims = [("1", "Avdelning"), ("6", "Projekt")]
        objekt = [("1", "100", "Adm"), ("1", "200", "Salg"), ("6", "9000300", "PX")]
        type_rows, member_rows = load_sie.sie_dim_analysis_rows(
            dims, objekt, company_id=32, now=self.NOW)
        self.assertEqual({r[2] for r in type_rows}, {"1", "6"})
        self.assertEqual(len(type_rows), 2)
        self.assertIn((32, "SIE", "1", "Avdelning", self.NOW), type_rows)
        self.assertEqual(len(member_rows), 3)
        # (company_id, source_format, analysis_type, analysis_id, desc, now)
        self.assertIn((32, "SIE", "1", "100", "Adm", self.NOW), member_rows)

    def test_empty_lists(self):
        self.assertEqual(load_sie.sie_dim_analysis_rows([], [], 1, self.NOW), ([], []))
```

- [ ] **Step 2: Kör — verifiera fail**

Run: `py -m unittest tests.test_load_sie.SieDimAnalysisRows -v`
Expected: FAIL (`AttributeError: module 'load_sie' has no attribute 'sie_dim_analysis_rows'`)

- [ ] **Step 3: Implementera builder i load_sie.py**

Lägg till efter `psaldo_fact_rows` (rad ~323):

```python
def sie_dim_analysis_rows(dims, objekt, company_id, now, source_format="SIE"):
    """SIE:s #DIM-axlar + #OBJEKT-medlemmar → (type_rows, member_rows), dedup.

    dims:   lista av (dim_nr, namn).
    objekt: lista av (dim_nr, objekt_nr, namn).
    type_rows:   (company_id, source_format, analysis_type, description, loaded_at)
    member_rows: (company_id, source_format, analysis_type, analysis_id, description, loaded_at)
    """
    types: dict = {}
    members: dict = {}
    for dim_nr, namn in dims:
        if dim_nr is None:
            continue
        types[dim_nr] = (company_id, source_format, dim_nr, namn, now)
    for dim_nr, objekt_nr, namn in objekt:
        if dim_nr is None or objekt_nr is None:
            continue
        members[(dim_nr, objekt_nr)] = (
            company_id, source_format, dim_nr, objekt_nr, namn, now)
    return list(types.values()), list(members.values())
```

- [ ] **Step 4: Kör — verifiera pass**

Run: `py -m unittest tests.test_load_sie.SieDimAnalysisRows -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add load_sie.py tests/test_load_sie.py
git commit -m "feat(sie-dims): sie_dim_analysis_rows-builder (#DIM/#OBJEKT → dim-tabellrader)"
```

---

## Task 4: Loader — analys-rader ur vouchers_to_journal_rows (period-paritet)

**Files:**
- Modify: `load_sie.py` (`vouchers_to_journal_rows` rad 56-89; caller rad 507-511)
- Test: `tests/test_load_sie.py`

- [ ] **Step 1: Skriv de fallerande testerna (mirror av test_load_saft_analysis)**

Lägg till i `tests/test_load_sie.py`:

```python
class VoucherAnalysisRows(unittest.TestCase):
    NOW = _dt(2026, 5, 29)

    def _parsed(self):
        return {
            "konto": {"7830": "Avskrivningar"},
            "vouchers": [{
                "series": "IN26", "number": "1", "date": "20260131", "text": "x",
                "transes": [
                    {"line_no": 1, "account": "7830", "amount": 1247.27,
                     "trans_text": "a", "quantity": None,
                     "analysis": [("1", "100"), ("6", "9000300")]},
                    {"line_no": 2, "account": "1209", "amount": -1247.27,
                     "trans_text": "b", "quantity": None, "analysis": []},
                ],
            }],
        }

    def test_analysis_rows_share_voucher_period(self):
        rows, analysis_rows, periods, skipped = load_sie.vouchers_to_journal_rows(
            self._parsed(), company_id=32, currency="SEK",
            rel_src="x.se", now=self.NOW)
        self.assertEqual(periods, {"202601"})
        # 2 dim-par på linje 1, 0 på linje 2 → 2 analys-rader.
        self.assertEqual(len(analysis_rows), 2)
        # tupel: (company_id, period, series, voucher_number, line_no,
        #         account_code, analysis_type, analysis_id, amount, currency,
        #         source_file, loaded_at)
        self.assertEqual(analysis_rows[0],
            (32, "202601", "IN26", "1", 1, "7830", "1", "100",
             1247.27, "SEK", "x.se", self.NOW))
        self.assertEqual(analysis_rows[1][6:9], ("6", "9000300", 1247.27))
        # Periodparitet: alla analys-perioder finns i journal-perioderna.
        self.assertTrue({r[1] for r in analysis_rows} <= periods)

    def test_cutoff_skips_journal_and_analysis(self):
        parsed = self._parsed()
        parsed["vouchers"][0]["date"] = "20260531"   # maj
        rows, analysis_rows, periods, skipped = load_sie.vouchers_to_journal_rows(
            parsed, 32, "SEK", "x.se", self.NOW, period_cutoff="202604")
        self.assertEqual(skipped, 1)
        self.assertEqual(rows, [])
        self.assertEqual(analysis_rows, [])
```

- [ ] **Step 2: Kör — verifiera fail**

Run: `py -m unittest tests.test_load_sie.VoucherAnalysisRows -v`
Expected: FAIL (`ValueError: not enough values to unpack (expected 4, got 3)`)

- [ ] **Step 3: Ändra vouchers_to_journal_rows att även returnera analys-rader**

Ersätt `vouchers_to_journal_rows` (rad 56-89) med:

```python
def vouchers_to_journal_rows(parsed: dict, company_id: int, currency: str,
                             rel_src: str, now: datetime,
                             period_cutoff: str | None = None
                             ) -> tuple[list[tuple], list[tuple], set[str], int]:
    """Plana ut vouchers → rader för fact_journal_sie OCH fact_sie_analysis.

    Analys-rader byggs i SAMMA loop som journalraderna och ärver linjens
    voucher-period (period = v["date"][:6]) → analysens periodisering kan
    aldrig divergera från journalens (skydd mot b711832-liknande bugg).

    period_cutoff: om satt, skippa vouchers vars period (YYYYMM) > cutoff —
    journal OCH analys droppas tillsammans.
    Returnerar (journal_rows, analysis_rows, periods, skipped_periods_count).
    """
    konto = parsed["konto"]
    rows: list[tuple] = []
    analysis_rows: list[tuple] = []
    periods: set[str] = set()
    skipped = 0
    for v in parsed["vouchers"]:
        d = v["date"]  # 'YYYYMMDD'
        period = d[:6]
        if period_cutoff and period > period_cutoff:
            skipped += 1
            continue
        periods.add(period)
        try:
            from datetime import date as _date
            voucher_date = _date(int(d[:4]), int(d[4:6]), int(d[6:8]))
        except (ValueError, IndexError):
            continue
        for t in v["transes"]:
            rows.append((
                company_id, period, v["series"], v["number"],
                voucher_date, v["text"], t["line_no"],
                t["account"], konto.get(t["account"]),
                t["amount"], t["trans_text"], t["quantity"],
                currency, rel_src, now,
            ))
            for dim_nr, objekt_nr in t.get("analysis", []):
                analysis_rows.append((
                    company_id, period, v["series"], v["number"], t["line_no"],
                    t["account"], dim_nr, objekt_nr, t["amount"],
                    currency, rel_src, now,
                ))
    return rows, analysis_rows, periods, skipped
```

- [ ] **Step 4: Uppdatera anroparen i load_file**

I `load_file` (rad 504-511), ändra:

```python
    journal_rows: list[tuple] = []
    journal_periods: set[str] = set()
    journal_skipped = 0
    if include_journal and parsed["vouchers"]:
        journal_rows, journal_periods, journal_skipped = vouchers_to_journal_rows(
            parsed, company_id, currency, rel_src, now,
            period_cutoff=period_override,
        )
```

till:

```python
    journal_rows: list[tuple] = []
    analysis_rows: list[tuple] = []
    journal_periods: set[str] = set()
    journal_skipped = 0
    if include_journal and parsed["vouchers"]:
        journal_rows, analysis_rows, journal_periods, journal_skipped = \
            vouchers_to_journal_rows(
                parsed, company_id, currency, rel_src, now,
                period_cutoff=period_override,
            )
```

- [ ] **Step 5: Kör hela test_load_sie + test_sie_parser**

Run: `py -m unittest tests.test_load_sie tests.test_sie_parser -v`
Expected: PASS (de nya + alla befintliga; bekräftar att den ändrade returtupeln inte bröt något).

- [ ] **Step 6: Commit**

```bash
git add load_sie.py tests/test_load_sie.py
git commit -m "feat(sie-dims): analys-rader ur vouchers_to_journal_rows (voucher-period-paritet)"
```

---

## Task 5: Loader — wire in upsert + insert + DELETE-paritet i load_file

**Files:**
- Modify: `load_sie.py` (`load_file` transaktionsblock, rad 533-665)

Detta steg är DB-rörande och täcks av integrationsskriptet i Task 7 (ingen DB-unit-harness finns). Verifiering här = befintliga unit-tester gröna + import-sanity.

- [ ] **Step 1: COPY/insert-konstant + upsert-SQL**

`fact_sie_analysis` skrivs via `executemany`-batch (samma stil som `fact_journal_sie`; ingen COPY). Ingen ny modulkonstant behövs.

- [ ] **Step 2: Upserta dim_analysis_type/_member (alltid, oberoende av include_journal)**

I `load_file`, inuti `con.execute("BEGIN")`-blocket, EFTER SIE/PSALDO-insert men INNAN journal-blocket (dvs efter rad ~586, före `if journal_periods:`), lägg till:

```python
        # Dimensioner: upserta axel-/medlemsnamn ur #DIM/#OBJEKT (best-effort,
        # ON CONFLICT). Körs oberoende av include_journal — deklarationerna finns
        # i filhuvudet även när journal hoppas över.
        sie_type_rows, sie_member_rows = sie_dim_analysis_rows(
            parsed.get("dims", []), parsed.get("objekt", []), company_id, now)
        if sie_type_rows:
            con.executemany(
                """INSERT INTO dim_analysis_type
                   (company_id, source_format, analysis_type, description, loaded_at)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (company_id, source_format, analysis_type)
                   DO UPDATE SET description = EXCLUDED.description,
                                 loaded_at = EXCLUDED.loaded_at""",
                sie_type_rows)
        if sie_member_rows:
            con.executemany(
                """INSERT INTO dim_analysis_member
                   (company_id, source_format, analysis_type, analysis_id,
                    description, loaded_at)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (company_id, source_format, analysis_type, analysis_id)
                   DO UPDATE SET description = EXCLUDED.description,
                                 loaded_at = EXCLUDED.loaded_at""",
                sie_member_rows)
```

- [ ] **Step 3: FY-override-DELETE för fact_sie_analysis**

I override-grenen (rad 539-550), EFTER `DELETE FROM fact_journal_sie ... period > %s ...`, lägg till speglande analys-DELETE:

```python
            con.execute(
                """DELETE FROM fact_sie_analysis
                   WHERE company_id = %s AND period > %s AND period BETWEEN %s AND %s""",
                [company_id, period, fy_start, fy_end],
            )
```

- [ ] **Step 4: Per-period-DELETE + INSERT för fact_sie_analysis**

I journal-blocket (rad 591-608), INUTI `if journal_periods:` EFTER journal-insert-loopen, lägg till (återanvänder samma `placeholders`/`jp_sorted`):

```python
            con.execute(
                f"""DELETE FROM fact_sie_analysis
                    WHERE company_id = %s AND period IN ({placeholders})""",
                [company_id, *jp_sorted],
            )
            for i in range(0, len(analysis_rows), JOURNAL_BATCH):
                con.executemany(
                    """INSERT INTO fact_sie_analysis
                       (company_id, period, series, voucher_number, line_no,
                        account_code, analysis_type, analysis_id, amount,
                        currency, source_file, loaded_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    analysis_rows[i:i + JOURNAL_BATCH],
                )
```

(`analysis_rows`-perioderna ⊆ `journal_periods` by construction → per-period-DELETE på `jp_sorted` täcker dem.)

- [ ] **Step 5: Lägg analys-antal i load_history-meddelandet + OK-loggen**

I `load_history`-INSERT-meddelandet (rad 659-663) lägg till `analysis_rows={len(analysis_rows)}`. I OK-loggraden (rad 680-682) lägg till `ANALYS={len(analysis_rows)}` i `journal_msg` (eller eget fält).

```python
    journal_msg = f" JOURNAL={len(journal_rows)}({len(journal_periods)} mån) ANALYS={len(analysis_rows)}" if journal_rows else ""
```

- [ ] **Step 6: Import-sanity + hela unit-sviten**

Run: `py -c "import load_sie; print('import OK')"`
Run: `py -m unittest tests.test_load_sie tests.test_sie_parser -v`
Expected: `import OK` + PASS (alla unit-tester; DB-vägen verifieras i Task 7).

- [ ] **Step 7: Commit**

```bash
git add load_sie.py
git commit -m "feat(sie-dims): wire dim-upsert + fact_sie_analysis insert/DELETE-paritet i load_file"
```

---

## Task 6: Grants-migration

**Files:**
- Create: `db/migrations/20260529_sie_analysis_grants.sql`

- [ ] **Step 1: Skriv migrationsfilen**

Spegla SAF-T-analys-migrationen (`20260528_analysis_dimension_tables.sql`). Skapa `db/migrations/20260529_sie_analysis_grants.sql`:

```sql
-- SIE-dimensioner: grants för fact_sie_analysis.
-- dim_analysis_type/_member har redan grants från SAF-T-migrationen.
-- Ingen PII (bara dim-nr/objekt-nr/belopp) → full SELECT till mcp_readonly.
GRANT SELECT ON fact_sie_analysis TO mcp_readonly;
GRANT SELECT, INSERT, DELETE ON fact_sie_analysis TO etl_writer;
GRANT USAGE, SELECT ON SEQUENCE seq_fact_sie_analysis TO etl_writer;
```

- [ ] **Step 2: Verifiera mot befintlig migrationskonvention**

Run: `py -c "from pathlib import Path; t=Path('db/migrations/20260529_sie_analysis_grants.sql').read_text(); assert 'mcp_readonly' in t and 'etl_writer' in t and 'seq_fact_sie_analysis' in t; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add db/migrations/20260529_sie_analysis_grants.sql
git commit -m "feat(sie-dims): grants-migration för fact_sie_analysis"
```

---

## Task 7: Manuell lokal-Postgres-integration

**Files:**
- Create: `scripts/verify_sie_analysis.py` (speglar `scripts/verify_saft_analysis.py`)

- [ ] **Step 1: Skriv verifieringsskriptet**

```python
"""Manuell integrationsverifiering: ladda en dim-tung SE-SIE till lokal Postgres
och kontrollera analys-lagret. RÖR ALDRIG PROD — kräver localhost-DATABASE_URL.

    docker start finance-pg-dev
    $env:DATABASE_URL = "postgresql://dev:dev@localhost:5432/finance"
    py scripts/verify_sie_analysis.py
"""
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402
from sie_parser import parse_sie, read_text_with_fallback  # noqa: E402

if "localhost" not in os.environ.get("DATABASE_URL", ""):
    sys.exit("VÄGRAR: DATABASE_URL pekar inte på localhost — kör bara mot lokal dev.")


def period_dist(path):
    """Periodfördelning journal vs analys ur SAMMA parse (samma nycklar förväntas)."""
    parsed = parse_sie(read_text_with_fallback(path), with_journal=True)
    jd, ad = Counter(), Counter()
    for v in parsed["vouchers"]:
        p = v["date"][:6]
        for t in v["transes"]:
            jd[p] += 1
            for _ in t.get("analysis", []):
                ad[p] += 1
    return jd, ad


def main():
    from shared import load_config
    from load_sie import build_orgnr_lookup, load_file, discover_files
    base = Path(load_config()["base_path"])
    # Välj en dim-tung SE-fil (t.ex. bolag 32 Axel Group eller en Hantverksdata-export).
    files = discover_files(base / "extracted/202604/Sweden")
    if not files:
        sys.exit("Inga SIE-filer i extracted/202604/Sweden")
    sie = files[0]
    con = db.connect(role="admin")
    try:
        db.init_schema(con)
        lookup = build_orgnr_lookup(con)
        load_file(con, sie, base, "202604", lookup, dry_run=False,
                  include_journal=True, override=[], force=False)
        n = con.execute("SELECT COUNT(*) FROM fact_sie_analysis").fetchone()[0]
        types = con.execute(
            "SELECT COUNT(*) FROM dim_analysis_type WHERE source_format='SIE'").fetchone()[0]
        members = con.execute(
            "SELECT COUNT(*) FROM dim_analysis_member WHERE source_format='SIE'").fetchone()[0]
        print(f"[OK] fact_sie_analysis={n} rader, dim_type(SIE)={types}, dim_member(SIE)={members}")
        # Idempotens: ladda om → oförändrat radantal.
        load_file(con, sie, base, "202604", lookup, dry_run=False,
                  include_journal=True, override=[], force=False)
        n2 = con.execute("SELECT COUNT(*) FROM fact_sie_analysis").fetchone()[0]
        print(f"[{'OK' if n2 == n else 'FAIL'}] idempotens: {n} -> {n2}")
        # Period-bindning: journal- och analys-fördelning ska ha samma periodnycklar.
        jd, ad = period_dist(sie)
        ok = set(ad) <= set(jd)   # analys ⊆ journal
        print(f"[{'OK' if ok else 'FAIL'}] period-bindning (analys ⊆ journal): "
              f"analys={sorted(ad)} journal={sorted(jd)}")
        # Sum-per-en-typ ≤ journaltotal (odimensionerad rest).
        row = con.execute(
            """SELECT analysis_type, SUM(amount) FROM fact_sie_analysis
               GROUP BY analysis_type ORDER BY analysis_type""").fetchall()
        print(f"[INFO] sum per analysis_type: {row}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Kör mot lokal Postgres**

```bash
docker start finance-pg-dev
$env:DATABASE_URL = "postgresql://dev:dev@localhost:5432/finance"
py scripts/verify_sie_analysis.py
```
Expected: `[OK] fact_sie_analysis=… rader …`, `[OK] idempotens: N -> N`, `[OK] period-bindning (analys ⊆ journal)`.

Om lokal Postgres saknar `extracted/`-data: ladda först en SE-fil dit, eller peka skriptet på en testfil. Notera resultatet; vid FAIL → felsök innan commit.

- [ ] **Step 3: Commit**

```bash
git add scripts/verify_sie_analysis.py
git commit -m "chore(scripts): manuell lokal-Postgres-verifiering av SIE-analys-lagret"
```

---

## Task 8: Dokumentation — warehouse_semantics.md + SCHEMA.md

**Files:**
- Modify: `docs/warehouse_semantics.md` (nytt mental model för SIE-analys)
- Modify: `SCHEMA.md` (dokumentera `fact_sie_analysis`)

- [ ] **Step 1: Läs SAF-T:s "Mental model 5" i warehouse_semantics.md**

Run: `py -c "import re,sys; t=open('docs/warehouse_semantics.md',encoding='utf-8').read(); i=t.find('fact_saft_analysis'); print(t[i-200:i+1200])"`
Expected: visar SAF-T-analys-avsnittet att spegla.

- [ ] **Step 2: Lägg till SIE-analys-avsnitt i warehouse_semantics.md**

Lägg ett nytt mental model (efter SAF-T:s) som dokumenterar `fact_sie_analysis`:

```markdown
### fact_sie_analysis — SIE-dimensioner (per linje × #DIM-axel)

`fact_sie_analysis` är SIE-spegeln av `fact_saft_analysis`: en rad per
(#TRANS-linje × dim-par). `analysis_type` = #DIM-nr, `analysis_id` = #OBJEKT-nr;
namn slås upp i `dim_analysis_type`/`dim_analysis_member` (`source_format='SIE'`).

Regler (samma femte fälla som SAF-T):
1. **SUM:a aldrig över `analysis_type`** — multi-dim upprepar hela linjebeloppet.
   Filtrera alltid på EN `analysis_type`.
2. `amount` är **linjenivå → månadsrörelse**, aldrig YTD. Jämför mot
   `fact_journal_sie` (månadsrörelse), aldrig mot `fact_balances` (YTD för SE).
3. **Odimensionerad rest:** `SUM(amount WHERE analysis_type=X) ≤ journaltotal` —
   konton/linjer utan objektlista (och bolag med tunna #VER) är otaggade.
4. period = verifikatets månad (= `fact_journal_sie`). Rollups: månad/YTD/helår/LTM
   via `WHERE period …`.
```

- [ ] **Step 3: Dokumentera fact_sie_analysis i SCHEMA.md**

Lägg en kort tabellbeskrivning analogt med `fact_saft_analysis` (kolumner + att den delar `dim_analysis_*` via `source_format='SIE'`).

- [ ] **Step 4: Commit**

```bash
git add docs/warehouse_semantics.md SCHEMA.md
git commit -m "docs(sie-dims): mental model + SCHEMA för fact_sie_analysis"
```

---

## Slutverifiering

- [ ] **Hela unit-sviten grön**

Run: `py -m unittest tests.test_sie_parser tests.test_load_sie -v`
Expected: PASS, inga regressioner i `TransDimensions`/`VoucherBalance`.

- [ ] **Lokal integration grön** (Task 7 körd, `[OK]` på alla rader).

- [ ] **SQL smoke-test** (om SQL rörts utöver migrationen): `py scripts/smoke_test_sql.py` mot Azure-DB enligt [[feedback_sql_smoke_test_before_push]].

- [ ] **Uppföljning noterad:** SIE-historik laddas om en gång via `load_history_sie_saft.py --format sie` (separat, efter prod-DDL+grants). Prod-utrullning = Didriks beslut.

---

## Self-review (ifylld)

- **Spec-täckning:** §1 datamodell→Task 1; §2 period→Task 4; §3 parser→Task 2; §4 loader→Task 3+5; §5 semantik→Task 8; §6 tester→Task 2/3/4/7; §7 migration→Task 6+slutverifiering. Alla sektioner täckta.
- **Placeholder-scan:** inga TBD/TODO; all kod konkret.
- **Typ-konsistens:** `vouchers_to_journal_rows` returnerar `(journal_rows, analysis_rows, periods, skipped)` i Task 4 och konsumeras med samma 4-tupel i Task 4 Step 4. Analys-tupelns kolumnordning (Task 4 Step 3) matchar INSERT-kolumnerna (Task 5 Step 4) och fact_sie_analysis-schemat (Task 1). `sie_dim_analysis_rows`-tupelformer (Task 3) matchar INSERT i Task 5 Step 2.
