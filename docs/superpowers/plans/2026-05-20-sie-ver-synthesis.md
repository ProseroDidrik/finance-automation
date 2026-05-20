# SIE_VER — syntetiserade månadssaldon från verifikat — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ge SE-bolag som saknar `#PSALDO` korrekt månadsfördelad P&L genom att kumulera verifikatraderna i `fact_journal_sie` till YTD-saldon och lagra dem som `source_kind='SIE_VER'`.

**Architecture:** `load_sie.py` får en ren kumfunktion (`cumulate_ytd`) plus `synthesize_sie_ver()` som körs inom den befintliga laddnings-transaktionen, efter att journalen skrivits. `report_pnl.sql` och `report_pivot.sql` får `SIE_VER` inlagd i sin `best_source`-prioritet (Sverige: `SIE_PSALDO → SIE_VER → SIE → IMP → IMP_ADJ`). Lager-isolering (`delete_db.py`, `db.py`) utökas med `SIE_VER`.

**Tech Stack:** Python 3.11, psycopg3, Azure Postgres, ad hoc-verifiering via `scripts/check_*.py` (repo-konvention — inget pytest).

---

## Avvikelser från specen (`2026-05-20`-specen) — läs först

Specen är ett *förslag* och verifierades mot koden 2026-05-20. Följande är **medvetet ändrat** i den här planen:

1. **`compare_coverage.sql` rörs INTE.** Den faktiska filen läser redan `fact_journal_sie` direkt för SIE-sidan (ingen `sie_pick`-CTE finns — den existerar bara i ett inaktuellt exempel i `warehouse_semantics.md`). compare_coverage har redan korrekt månadsfördelning. Samma sak för `coverage_accounts.sql`.
2. **Teckenkonvention:** `fact_journal_sie.amount` är i SIE-konvention (intäkt negativ — empiriskt verifierat: bolag 4 / konto 3041 jan = −1 250 077). `SIE_VER.amount = SUM(amount)` rakt av, **ingen** sign-flip. Specens acceptanskriterium 1 & 2 (`SUM(-amount)`) var fel och är rättat nedan.
3. **`report_pivot.sql` läggs till** — den har en identisk `best_source`-CTE som specen missade.
4. **`delete_db.py` + `db.py` lager-tupler** läggs till — specen nämnde bara `db.py`-schemat.
5. **Acceptanskriterium 3 omformulerat** — eftersom `compare_coverage.sql` redan är journalbaserad mäts ingen mismatch-förändring där; kriteriet blir "compare_coverage-resultatet är oförändrat".
6. **best_source-ordning:** faktisk nuvarande ordning är `SIE → SIE_PSALDO → IMP → IMP_ADJ`. Vald lösning (bekräftad med användaren) är `SIE_PSALDO → SIE_VER → SIE → IMP → IMP_ADJ` — de 14 #PSALDO-bolagen byter därmed från #RES-baserad `SIE` till källrapporterad `SIE_PSALDO` i P&L (liten, korrekt förbättring).
7. **Syntesfunktionen läser `fact_journal_sie` från DB** (inte in-memory `parsed["vouchers"]`) — bolag 4 har jan–mar i separata månadsfiler, så aprilfilens vouchers räcker inte för YTD-kumen.
8. **CA-bolag (49, 162) utanför scope** — syntesen gatas på `country='Sweden'`. CA kan följas upp separat.

---

## File Structure

| Fil | Ansvar | Ändring |
|---|---|---|
| `load_sie.py` | SIE-laddning | Ny ren funktion `cumulate_ytd()` + `fy_periods()`; ny `synthesize_sie_ver()`; ny konstant `SOURCE_KIND_SIE_VER`; `build_orgnr_lookup()` returnerar även `country`; anrop i `load_file()` efter journal-INSERT. |
| `webapp/backend/sql/report_pnl.sql` | P&L-rapport | `best_source`-CTE: lägg `SIE_VER` i Sverige-grenen, ny ordning. |
| `webapp/backend/sql/report_pivot.sql` | Pivot-rapport | Samma `best_source`-ändring. |
| `delete_db.py` | Radering / lager-isolering | SE/CA IMP-target: lägg `SIE_VER` i `fact_balances`-source_kinds. |
| `db.py` | Schema + lager-konstanter | `IMP_KINDS_BY_COUNTRY` (Sweden+CA) och `IMP_KINDS` får `SIE_VER`; `source_kind`-kommentar uppdateras. |
| `scripts/check_sie_ver.py` | Verifiering (NY) | Ren kumlogik-asserts + DB-integration mot bolag 4 + coverage-query. |
| `docs/warehouse_semantics.md` | Query-guide | Mental model 2 (ny prioritet + `SIE_VER`); uppdatera #PSALDO-brus-sektionen. |
| `SCHEMA.md` | Schemadok | Lägg `SIE_VER` i source_kind-uppräkningen. |

---

## Task 1: Ren kumlogik (`fy_periods` + `cumulate_ytd`)

**Files:**
- Create: `scripts/check_sie_ver.py`
- Modify: `load_sie.py` (nya funktioner efter `vouchers_to_journal_rows`, ca rad 233)

- [ ] **Step 1: Skriv den failande checken**

Skapa `scripts/check_sie_ver.py`:

```python
"""Verifiera SIE_VER-syntesen: ren kumlogik + DB-integration mot bolag 4.

Kör:  py scripts/check_sie_ver.py
Exit 0 = allt OK, 1 = minst ett fel.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db
from load_sie import cumulate_ytd, fy_periods


def check_fy_periods() -> bool:
    ok = fy_periods("202601", "202604") == ["202601", "202602", "202603", "202604"]
    ok = ok and fy_periods("202603", "202603") == ["202603"]
    print(f"[{'OK' if ok else 'FAIL'}]  fy_periods")
    return ok


def check_cumulate() -> bool:
    # 2 konton, 3 månader. Konto 3000 aktivt jan+mar, konto 4000 bara feb.
    periods = ["202601", "202602", "202603"]
    monthly = [
        ("3000", "202601", -100.0),
        ("3000", "202603",  -50.0),
        ("4000", "202602",   30.0),
    ]
    got = {(c, p): round(v, 2) for c, p, v in cumulate_ytd(monthly, periods)}
    want = {
        ("3000", "202601"): -100.0,   # första aktivitet
        ("3000", "202602"): -100.0,   # carry-forward (ingen rörelse feb)
        ("3000", "202603"): -150.0,   # +(-50)
        ("4000", "202602"):   30.0,   # första aktivitet
        ("4000", "202603"):   30.0,   # carry-forward
    }
    ok = got == want
    print(f"[{'OK' if ok else 'FAIL'}]  cumulate_ytd")
    if not ok:
        print(f"  want={want}\n  got ={got}")
    return ok


def check_db_company4() -> bool:
    con = db.connect(read_only=True)
    try:
        ver = con.execute(
            """SELECT amount FROM fact_balances
               WHERE company_id = 4 AND account_code = '3041'
                 AND source_kind = 'SIE_VER' AND period = '202604'"""
        ).fetchone()
        jrnl = con.execute(
            """SELECT SUM(amount) FROM fact_journal_sie
               WHERE company_id = 4 AND account_code = '3041'
                 AND period BETWEEN '202601' AND '202604'"""
        ).fetchone()
        if ver is None:
            print("[FAIL]  bolag 4 3041: ingen SIE_VER-rad för 202604 "
                  "(har load_sie.py körts?)")
            return False
        diff = abs(ver[0] - (jrnl[0] or 0.0))
        ok = diff < 1.0
        print(f"[{'OK' if ok else 'FAIL'}]  bolag 4 3041 SIE_VER YTD apr "
              f"= {ver[0]:.2f}  journal jan..apr = {jrnl[0]:.2f}  diff = {diff:.2f}")
        return ok
    finally:
        con.close()


def check_coverage() -> bool:
    con = db.connect(read_only=True)
    try:
        rows = con.execute(
            """WITH har_psaldo AS (SELECT DISTINCT company_id FROM fact_balances
                                   WHERE source_kind = 'SIE_PSALDO' AND scenario = 'A'),
                    har_sie_ver AS (SELECT DISTINCT company_id FROM fact_balances
                                    WHERE source_kind = 'SIE_VER' AND scenario = 'A'),
                    har_sie AS (SELECT DISTINCT company_id FROM fact_balances
                                WHERE source_kind = 'SIE' AND scenario = 'A')
               SELECT c.company_id, c.name FROM dim_company c
               WHERE c.country = 'Sweden'
                 AND c.company_id IN (SELECT company_id FROM har_sie)
                 AND c.company_id NOT IN (SELECT company_id FROM har_psaldo)
                 AND c.company_id NOT IN (SELECT company_id FROM har_sie_ver)"""
        ).fetchall()
        n = len(rows)
        tag = "OK" if n == 0 else "INFO"
        suffix = "" if n == 0 else " (väntat tills full SE-omladdning körts — rollout steg 1)"
        print(f"[{tag}]  coverage: {n} SE-bolag med SIE men utan PSALDO/SIE_VER{suffix}")
        for cid, nm in rows[:10]:
            print(f"           {cid}  {nm}")
        return n == 0
    finally:
        con.close()


def main() -> None:
    # Deterministiska checkar gatar exit-koden. check_coverage är informativ —
    # den ger 0 först efter full SE-omladdning (rollout steg 1), inte mitt i
    # planen då bara bolag 4 laddats om.
    hard = [check_fy_periods(), check_cumulate(), check_db_company4()]
    check_coverage()
    sys.exit(0 if all(hard) else 1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Kör checken — verifiera att den failar**

Run: `py scripts/check_sie_ver.py`
Expected: FAIL med `ImportError: cannot import name 'cumulate_ytd' from 'load_sie'`.

- [ ] **Step 3: Implementera `fy_periods` + `cumulate_ytd` i `load_sie.py`**

Lägg till efter `vouchers_to_journal_rows()` (ca rad 233), före `derive_fy_range()`:

```python
def fy_periods(fy_start: str, period: str) -> list[str]:
    """Lista kalendermånader 'YYYYMM' från fy_start t.o.m. period (inklusive).

    Antar kalenderårs-progression. Anroparen ska redan ha avvisat brutet
    räkenskapsår (fy_start som inte slutar på '01').
    """
    out: list[str] = []
    y, m = int(fy_start[:4]), int(fy_start[4:6])
    while True:
        p = f"{y:04d}{m:02d}"
        out.append(p)
        if p >= period:
            break
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def cumulate_ytd(monthly_rows, periods: list[str]) -> list[tuple[str, str, float]]:
    """Kumulera månadsrörelse → YTD-saldo per konto.

    monthly_rows: iterable av (account_code, period, amount) — månadsrörelse.
    periods:      ordnad lista FY-perioder 'YYYYMM' (från fy_periods()).

    Returnerar list[(account_code, period, ytd_amount)]. Varje konto får en rad
    för varje period FRÅN sin första aktivitetsmånad och framåt (carry-forward),
    så att report_pnl.sql:s YTD-diff fungerar även för en månad utan rörelse.
    Tecknet bevaras (SIE-konvention — samma som fact_journal_sie).
    """
    by_acct: dict[str, dict[str, float]] = {}
    for account_code, p, amount in monthly_rows:
        acct = by_acct.setdefault(account_code, {})
        acct[p] = acct.get(p, 0.0) + amount

    period_index = {p: i for i, p in enumerate(periods)}
    out: list[tuple[str, str, float]] = []
    for account_code, mvm in by_acct.items():
        active = [period_index[p] for p in mvm if p in period_index]
        if not active:
            continue
        running = 0.0
        for i in range(min(active), len(periods)):
            running += mvm.get(periods[i], 0.0)
            out.append((account_code, periods[i], running))
    return out
```

- [ ] **Step 4: Kör checken — de rena checkarna ska passera**

Run: `py scripts/check_sie_ver.py`
Expected: `[OK]  fy_periods` och `[OK]  cumulate_ytd`. `check_db_company4` är `[FAIL]` och `coverage` visar `[INFO]` (SIE_VER finns inte i DB ännu) → exit 1. Det är väntat i detta steg.

- [ ] **Step 5: Commit**

```bash
git add scripts/check_sie_ver.py load_sie.py
git commit -m "feat(load_sie): ren YTD-kumlogik för SIE_VER-syntes"
```

---

## Task 2: `synthesize_sie_ver()` + inkoppling i `load_file`

**Files:**
- Modify: `load_sie.py` (ny konstant; `build_orgnr_lookup`; ny funktion; `load_file`)

- [ ] **Step 1: Lägg till konstant**

I `load_sie.py`, efter `SOURCE_KIND_PSALDO = "SIE_PSALDO"` (rad 39):

```python
SOURCE_KIND_SIE_VER = "SIE_VER"
```

- [ ] **Step 2: Låt `build_orgnr_lookup` returnera country**

Ersätt `build_orgnr_lookup()` (rad 262–278) helt:

```python
def build_orgnr_lookup(con: db.Conn) -> dict[str, tuple[int, str, str]]:
    """orgnr_normalized → (company_id, name, country) för alla bolag med orgnr.

    SIE är ett svenskt format så valutan är alltid SEK; vi tar ingen valuta
    från dim_company här (vissa CENTR/CA-bolag har svenskt orgnr men annan
    klassad valuta). country behövs för att gata SIE_VER-syntesen till Sverige.
    """
    lookup: dict[str, tuple[int, str, str]] = {}
    for row in con.execute(
        "SELECT company_id, name, country, orgnr FROM dim_company "
        "WHERE orgnr IS NOT NULL AND orgnr <> ''"
    ).fetchall():
        cid, name, country, orgnr = row
        key = normalize_orgnr(orgnr)
        if key:
            lookup[key] = (cid, name, country)
    return lookup
```

- [ ] **Step 3: Uppdatera uppackningen i `load_file`**

I `load_file()`, ersätt raden `company_id, _name = hit` (rad 316):

```python
    company_id, _name, country = hit
```

- [ ] **Step 4: Implementera `synthesize_sie_ver()`**

Lägg till i `load_sie.py` efter `cumulate_ytd()` (från Task 1):

```python
# Kontoklass 3–8 = resultaträkning (IS). 1–2 = balansräkning (BS) och kan inte
# YTD-kumuleras utan korrekt ingående balans — skippas i SIE_VER.
IS_ACCOUNT_CLASSES = ("3", "4", "5", "6", "7", "8")


def synthesize_sie_ver(con, company_id: int, fy_start: str, fy_end: str,
                       period: str, rel_src: str, now: datetime) -> int:
    """Syntetisera SIE_VER-rader (YTD-saldon) från fact_journal_sie.

    Anropas inom den öppna transaktionen i load_file, EFTER att journalraderna
    skrivits — läser därför både den aktuella filens verifikat och tidigare
    laddade månader. Aggregerar verifikat per (konto, period), kumulerar till
    YTD och skriver source_kind='SIE_VER'. Bara IS-konton (kontoklass 3–8).

    DELETE täcker hela FY:t (fy_start..fy_end) → idempotent och rensar även
    ev. stale senare-månadsrader. INSERT skrivs bara för fy_start..period.

    Returnerar antal SIE_VER-rader som skrevs (0 om inga verifikat finns —
    då behålls #RES-baserad SIE som fallback via best_source).
    """
    periods = fy_periods(fy_start, period)

    journal = con.execute(
        """SELECT account_code, period, SUM(amount) AS amount
           FROM fact_journal_sie
           WHERE company_id = %s
             AND period BETWEEN %s AND %s
             AND LEFT(account_code, 1) IN ('3','4','5','6','7','8')
           GROUP BY account_code, period""",
        [company_id, fy_start, period],
    ).fetchall()

    name_rows = con.execute(
        """SELECT account_code, MAX(account_name) AS account_name
           FROM fact_journal_sie
           WHERE company_id = %s AND period BETWEEN %s AND %s
           GROUP BY account_code""",
        [company_id, fy_start, period],
    ).fetchall()
    names = {code: nm for code, nm in name_rows}

    # Idempotens: rensa hela FY:t innan INSERT.
    con.execute(
        """DELETE FROM fact_balances
           WHERE company_id = %s AND source_kind = %s
             AND period BETWEEN %s AND %s""",
        [company_id, SOURCE_KIND_SIE_VER, fy_start, fy_end],
    )

    ytd = cumulate_ytd(journal, periods)
    if not ytd:
        return 0

    idx_per_period: dict[str, int] = {}
    insert_rows: list[tuple] = []
    for account_code, p, amount in ytd:
        idx_per_period[p] = idx_per_period.get(p, 0) + 1
        insert_rows.append((
            company_id, p, PERIOD_TYPE, account_code, names.get(account_code),
            amount, "SEK", "IS", SOURCE_KIND_SIE_VER, rel_src,
            idx_per_period[p], now,
        ))
    con.executemany(
        """INSERT INTO fact_balances
           (company_id, period, period_type, account_code, account_name,
            amount, currency, statement_type, source_kind, source_file,
            row_index, loaded_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        insert_rows,
    )
    return len(insert_rows)
```

- [ ] **Step 5: Anropa syntesen i `load_file` — efter journal-INSERT, före `load_history`-INSERT**

I `load_file()`, inuti `try`-blocket. Lägg till **direkt efter** journal-INSERT-blocket (efter `con.executemany(... journal_rows[i:i + JOURNAL_BATCH] ...)`, ca rad 545) och **före** `con.execute("INSERT INTO load_history ...")` (rad 547):

```python
        # SIE_VER: syntetisera YTD-saldon från verifikaten för SE-bolag som
        # saknar #PSALDO. #RES-fältet är en snapshot vid genereringstiden och
        # ger skev månadsfördelning; verifikat-kumen ger exakt fördelning.
        sie_ver_count = 0
        if include_journal and country == "Sweden" and not parsed["psaldo"]:
            if fy_start.endswith("01"):
                sie_ver_count = synthesize_sie_ver(
                    con, company_id, fy_start, fy_end, period, rel_src, now)
                if sie_ver_count == 0:
                    log("INFO", company_id,
                        "SIE_VER: inga verifikat i fact_journal_sie — "
                        "behåller #RES-baserad SIE som fallback.")
            else:
                log("WARN", company_id,
                    f"SIE_VER: brutet räkenskapsår (FY-start {fy_start}) — "
                    "hoppar över syntes (YTD-kum antar kalenderår).")
        elif country == "Sweden" and parsed["psaldo"]:
            # Bolaget levererar #PSALDO — rensa ev. stale SIE_VER från en
            # tidigare laddning då filen saknade #PSALDO. best_source föredrar
            # SIE_PSALDO så det är ofarligt numeriskt, men håll datat rent.
            con.execute(
                """DELETE FROM fact_balances
                   WHERE company_id = %s AND source_kind = %s
                     AND period BETWEEN %s AND %s""",
                [company_id, SOURCE_KIND_SIE_VER, fy_start, fy_end],
            )
```

Uppdatera sedan `load_history`-INSERT:ens `message`-sträng (rad 555–558) — lägg `sie_ver_rows` i f-strängen:

```python
             f"sie_rows={len(sie_rows)} psaldo_rows={len(psaldo_rows)} "
             f"psaldo_periods={len(psaldo_periods)} "
             f"journal_rows={len(journal_rows)} journal_periods={len(journal_periods)} "
             f"sie_ver_rows={sie_ver_count} "
             f"sum_ub={total_ub:.2f} sum_res={total_res:.2f}",
```

- [ ] **Step 6: Lägg `SIE_VER` i slut-loggraden**

Ersätt `journal_msg`-raden (rad 568) och lägg till en `sie_ver_msg`:

```python
    journal_msg = f" JOURNAL={len(journal_rows)}({len(journal_periods)} mån)" if journal_rows else ""
    sie_ver_msg = f" SIE_VER={sie_ver_count}" if sie_ver_count else ""
```

och lägg `{sie_ver_msg}` i den avslutande `log("OK", ...)`-strängen direkt efter `{journal_msg}`.

- [ ] **Step 7: Syntaxkontroll**

Run: `py -c "import load_sie"`
Expected: ingen output, exit 0 (ingen SyntaxError / ImportError).

- [ ] **Step 8: Commit**

```bash
git add load_sie.py
git commit -m "feat(load_sie): syntetisera SIE_VER från verifikat när #PSALDO saknas"
```

---

## Task 3: `best_source`-prioritet i report_pnl.sql + report_pivot.sql

**Files:**
- Modify: `webapp/backend/sql/report_pnl.sql:49-52`
- Modify: `webapp/backend/sql/report_pivot.sql:66-69`

- [ ] **Step 1: report_pnl.sql — ersätt Sverige-grenen i `best_source`**

Ersätt exakt dessa fyra rader (rad 49–52):

```sql
          WHEN MAX(CASE WHEN fb.source_kind = 'SIE'        THEN 1 ELSE 0 END) = 1 THEN 'SIE'
          WHEN MAX(CASE WHEN fb.source_kind = 'SIE_PSALDO' THEN 1 ELSE 0 END) = 1 THEN 'SIE_PSALDO'
          WHEN MAX(CASE WHEN fb.source_kind = 'IMP'        THEN 1 ELSE 0 END) = 1 THEN 'IMP'
          WHEN MAX(CASE WHEN fb.source_kind = 'IMP_ADJ'    THEN 1 ELSE 0 END) = 1 THEN 'IMP_ADJ'
```

med:

```sql
          WHEN MAX(CASE WHEN fb.source_kind = 'SIE_PSALDO' THEN 1 ELSE 0 END) = 1 THEN 'SIE_PSALDO'
          WHEN MAX(CASE WHEN fb.source_kind = 'SIE_VER'    THEN 1 ELSE 0 END) = 1 THEN 'SIE_VER'
          WHEN MAX(CASE WHEN fb.source_kind = 'SIE'        THEN 1 ELSE 0 END) = 1 THEN 'SIE'
          WHEN MAX(CASE WHEN fb.source_kind = 'IMP'        THEN 1 ELSE 0 END) = 1 THEN 'IMP'
          WHEN MAX(CASE WHEN fb.source_kind = 'IMP_ADJ'    THEN 1 ELSE 0 END) = 1 THEN 'IMP_ADJ'
```

- [ ] **Step 2: report_pivot.sql — ersätt Sverige-grenen i `best_source`**

Ersätt exakt dessa fyra rader (rad 66–69, 20 mellanslags indrag):

```sql
                    WHEN MAX(CASE WHEN fb.source_kind = 'SIE'        THEN 1 ELSE 0 END) = 1 THEN 'SIE'
                    WHEN MAX(CASE WHEN fb.source_kind = 'SIE_PSALDO' THEN 1 ELSE 0 END) = 1 THEN 'SIE_PSALDO'
                    WHEN MAX(CASE WHEN fb.source_kind = 'IMP'        THEN 1 ELSE 0 END) = 1 THEN 'IMP'
                    WHEN MAX(CASE WHEN fb.source_kind = 'IMP_ADJ'    THEN 1 ELSE 0 END) = 1 THEN 'IMP_ADJ'
```

med:

```sql
                    WHEN MAX(CASE WHEN fb.source_kind = 'SIE_PSALDO' THEN 1 ELSE 0 END) = 1 THEN 'SIE_PSALDO'
                    WHEN MAX(CASE WHEN fb.source_kind = 'SIE_VER'    THEN 1 ELSE 0 END) = 1 THEN 'SIE_VER'
                    WHEN MAX(CASE WHEN fb.source_kind = 'SIE'        THEN 1 ELSE 0 END) = 1 THEN 'SIE'
                    WHEN MAX(CASE WHEN fb.source_kind = 'IMP'        THEN 1 ELSE 0 END) = 1 THEN 'IMP'
                    WHEN MAX(CASE WHEN fb.source_kind = 'IMP_ADJ'    THEN 1 ELSE 0 END) = 1 THEN 'IMP_ADJ'
```

- [ ] **Step 3: Smoke-testa SQL-syntax mot dev-DB**

`SIE_VER` är `period_type='ytd'` så de befintliga ytd-grenarna i `balances`/`month_amounts` (YTD-diff) gäller oförändrat — inga andra CTE:n behöver röras. Verifiera att filerna parsar genom att köra repots SQL-smoke-test (`DATABASE_URL` måste peka på dev-DB):

Run: `py scripts/smoke_test_sql.py`
Expected: inga psycopg-syntaxfel för `report_pnl.sql` / `report_pivot.sql`.

> Om `smoke_test_sql.py` inte täcker dessa filer: hoppa över detta steg, syntaxen valideras i Task 5 när rapporten körs mot bolag 4.

- [ ] **Step 4: Commit**

```bash
git add webapp/backend/sql/report_pnl.sql webapp/backend/sql/report_pivot.sql
git commit -m "feat(report): SIE_VER vinner över SIE i best_source (SE)"
```

---

## Task 4: Lager-isolering — delete_db.py + db.py

**Files:**
- Modify: `delete_db.py:77`
- Modify: `db.py:29-38` (lager-konstanter) + `db.py:216` (kommentar)

- [ ] **Step 1: delete_db.py — lägg SIE_VER i SE/CA IMP-target**

I `_delete_targets_for_company()`, ersätt raden (rad 77):

```python
                ("fact_balances",     ["SIE", "SIE_PSALDO"], "fy"),
```

med:

```python
                ("fact_balances",     ["SIE", "SIE_PSALDO", "SIE_VER"], "fy"),
```

- [ ] **Step 2: db.py — lägg SIE_VER i lager-konstanterna**

Ersätt `IMP_KINDS_BY_COUNTRY` + `IMP_KINDS` (rad 29–38):

```python
IMP_KINDS_BY_COUNTRY = {
    "Sweden":  ("SIE", "SIE_PSALDO", "SIE_VER"),
    "CA":      ("SIE", "SIE_PSALDO", "SIE_VER"),
    "Norway":  ("SAFT",),
    "Finland": ("IMP",),
    "Denmark": ("IMP",),
    "Germany": ("IMP",),
    "CENTR":   ("IMP",),
}
IMP_KINDS = ("IMP", "SIE", "SIE_PSALDO", "SIE_VER", "SAFT")
```

- [ ] **Step 3: db.py — uppdatera source_kind-kommentaren**

Ersätt kommentaren på `source_kind`-raden i `CREATE TABLE fact_balances` (rad 216):

```python
    source_kind     TEXT NOT NULL,         -- 'IMP'|'SIE'|'SIE_PSALDO'|'SIE_VER'|'SAFT'|'MAN'|'IMP_ADJ'|'IB'
```

- [ ] **Step 4: Syntaxkontroll**

Run: `py -c "import db, delete_db"`
Expected: ingen output, exit 0.

- [ ] **Step 5: Commit**

```bash
git add delete_db.py db.py
git commit -m "chore: registrera SIE_VER i lager-isoleringen (delete_db, db)"
```

---

## Task 5: Integration — kör load_sie.py och verifiera mot bolag 4

**Files:** inga (verifiering)

> Förutsättning: `DATABASE_URL` pekar på en dev-databas med 2026-data (lokal Docker-Postgres `docker compose up -d postgres`, eller dev-instans). Bolag 4:s SIE-fil(er) måste vara åtkomliga för `load_sie.py` (i `extracted/{period}/Sweden/` eller via `--source-dir`).

- [ ] **Step 1: Ladda om bolag 4 med syntes**

Kör om SIE-laddningen för bolag 4 så SIE_VER skapas (filen läses på nytt, journalen finns redan, syntesen kör):

Run: `py load_sie.py --period 202604 --override 4`
Expected: `[OK] 4 ... SIE_VER=<N>` i loggen, `N > 0`.

> Om aprilfilen inte ligger kvar: kör mot den period vars fil finns, eller `--source-dir` mot rätt mapp. SIE_VER byggs alltid för hela FY:t från `fact_journal_sie`, oavsett vilken månadsfil som triggar körningen.

- [ ] **Step 2: Kör verifieringsskriptet**

Run: `py scripts/check_sie_ver.py`
Expected: exit 0. `fy_periods`, `cumulate_ytd` och `bolag 4 3041` är `[OK]` — särskilt:
`[OK]  bolag 4 3041 SIE_VER YTD apr = -7225417.xx  journal jan..apr = -7225417.xx  diff = 0.xx`
`coverage`-raden visar `[INFO]` med ~34 ännu icke-omladdade bolag — väntat här; den blir `[OK]` först efter rollout steg 1 (full SE-omladdning).

- [ ] **Step 3: Verifiera månadsfördelningen per period (acceptanskriterium 1)**

Kör denna query mot dev-DB (via `py scripts/check_sie_ver.py` är YTD redan testat; här kollas per-periods-YTD mot journal-kum):

```sql
SELECT v.period,
       v.amount AS sie_ver_ytd,
       (SELECT SUM(j.amount) FROM fact_journal_sie j
        WHERE j.company_id = 4 AND j.account_code = '3041'
          AND j.period BETWEEN '202601' AND v.period) AS journal_ytd
FROM fact_balances v
WHERE v.company_id = 4 AND v.account_code = '3041'
  AND v.source_kind = 'SIE_VER'
ORDER BY v.period;
```

Expected: `sie_ver_ytd = journal_ytd` (inom 1 SEK) för alla perioder 202601–202604.

- [ ] **Step 4: Verifiera P&L-rapporten väljer SIE_VER**

Kör `report_pnl.sql` för bolag 4, period 202604 (via webapp-endpointen `/api/report/pnl` eller direkt med filens 10 positionsparametrar). Bekräfta att konto 3041:s `amount_month` för april ≈ 1 380 886 (journalens april-rörelse), inte ≈ 1 547 930 (#RES-baserad).

- [ ] **Step 5: Ingen regression för #PSALDO-bolag**

Välj ett bolag *med* `#PSALDO` (kör `SELECT DISTINCT company_id FROM fact_balances WHERE source_kind='SIE_PSALDO' LIMIT 1`). Bekräfta att det bolaget saknar `SIE_VER`-rader (syntesen gatas på `not parsed["psaldo"]`) och att `best_source` väljer `SIE_PSALDO` för det:

```sql
SELECT source_kind, COUNT(*) FROM fact_balances
WHERE company_id = <PSALDO_BOLAG> AND scenario = 'A'
  AND source_kind IN ('SIE','SIE_PSALDO','SIE_VER')
GROUP BY source_kind;
```

Expected: `SIE_VER`-raden = 0.

- [ ] **Step 6: Commit (om något justerats under verifieringen — annars hoppa över)**

```bash
git add -A && git commit -m "test: verifiera SIE_VER mot bolag 4"
```

---

## Task 6: Dokumentation

**Files:**
- Modify: `docs/warehouse_semantics.md`
- Modify: `SCHEMA.md`

- [ ] **Step 1: warehouse_semantics.md — Mental model 2 (prioritetstabell)**

Ersätt Sverige- och CA-raderna i prioritetstabellen under "Mental model 2":

```
| Sweden | `SIE_PSALDO` → `SIE_VER` → `SIE` → `IMP` → `IMP_ADJ` |
```

(CA-raden lämnas oförändrad — SIE_VER syntetiseras inte för CA i denna iteration.)

- [ ] **Step 2: warehouse_semantics.md — ersätt SIE_PSALDO-stycket**

Ersätt stycket som börjar `` `SIE_PSALDO` = `#PSALDO`-raderna... `` med:

```
`SIE_PSALDO` = `#PSALDO`-raderna i SIE-filen (källrapporterat per-månads YTD-saldo) — bäst när det finns. `SIE_VER` = YTD-saldon syntetiserade av `load_sie.py` från verifikaten (`#VER`/`#TRANS`) för de ~35 SE-bolag som saknar `#PSALDO`; ger exakt månadsfördelning. `SIE` (#RES-baserad) är därmed effektivt deprekerad — kvar bara som sista fallback om verifikat-syntesen inte kunnat köras. När både finns: `SIE_PSALDO` > `SIE_VER` > `SIE`.
```

- [ ] **Step 3: warehouse_semantics.md — uppdatera "Förväntat brus"-sektionen**

I sektionen "Förväntat brus i SIE-jämförelse: `#PSALDO`-frånvaron", lägg till en notis i början:

```
> **Uppdaterat 2026-05-20:** `load_sie.py` syntetiserar numera `SIE_VER` (YTD
> kumulerat från verifikaten) för bolag utan `#PSALDO`. `report_pnl.sql` och
> `report_pivot.sql` väljer `SIE_VER` före `SIE`, så #RES-timing-bruset nedan
> gäller inte längre P&L-rapporterna. `compare_coverage.sql` påverkas inte —
> den läste redan `fact_journal_sie` direkt.
```

- [ ] **Step 4: SCHEMA.md — lägg SIE_VER i source_kind-uppräkningen**

Sök efter `source_kind`-listan i `SCHEMA.md` och lägg till `SIE_VER` (`ytd`, SE, syntetiserad av `load_sie.py` från verifikat). Matcha den befintliga tabellens format.

- [ ] **Step 5: Commit**

```bash
git add docs/warehouse_semantics.md SCHEMA.md
git commit -m "docs: dokumentera SIE_VER och ny best_source-prioritet"
```

---

## Acceptanskriterier (rättade mot specen)

1. **Korrekthet:** Bolag 4, konto 3041, perioder 202601–202604: `fact_balances.SIE_VER.amount` matchar kumulerad `SUM(fact_journal_sie.amount)` jan..period inom 1 SEK. (Task 5, Step 2–3.) — *Ingen sign-flip; båda är SIE-konvention.*
2. **Coverage:** Efter omladdning av 2026 har alla SE-bolag som har `SIE` men saknar `SIE_PSALDO` även `SIE_VER`-rader. (`check_coverage()` → 0 rader.)
3. **Ingen compare_coverage-regression:** `compare_coverage.sql` rörs inte och dess resultat är oförändrat (den var redan journalbaserad).
4. **Ingen P&L-regression för #PSALDO-bolag:** bolag med `#PSALDO` får inga `SIE_VER`-rader och `best_source` väljer `SIE_PSALDO` för dem. (Task 5, Step 5.)

## Rollout (efter merge — manuella steg, ingår inte i koden)

1. Ladda om 2026-historiken för SE: `py load_sie.py --period 202604 --override` (syntetiserar `SIE_VER` för alla 35 #PSALDO-lösa bolag). Kör per period om filerna är månadsuppdelade.
2. Kör `py scripts/check_sie_ver.py` — `check_coverage()` ska ge 0 rader.
3. Notifiera teamet: `source_kind='SIE_VER'` finns nu och `best_source`-prioriteten för SE har ändrats (relevant för custom SQL).

## Kända begränsningar / TODO

- **BS-konton (klass 1–2):** ej syntetiserade — YTD-kum kräver ingående balans. P&L-rapporten är inte påverkad.
- **CA-bolag (49, 162):** utanför scope — syntesen gatas på `country='Sweden'`. Separat uppföljning vid behov.
- **Brutet räkenskapsår:** `synthesize_sie_ver` hoppas över med WARN om FY-start inte är januari (inga sådana bolag idag).
- **`--no-include-journal`:** ingen syntes — `#RES`-baserad `SIE` kvarstår som fallback.
- **Historiska SIE-filer:** `load_history_sie_saft.py` anropar `load_sie.load_file` → syntesen ärvs automatiskt, men `--include-journal` är **opt-in** där (default av, till skillnad från `load_sie.py` där det är default på). Med `--include-journal` ger en historisk årsfil utan `#PSALDO` 12 månatliga `SIE_VER`-rader kumulerade från årets verifikat — avsiktligt (ger månadsgranularitet åt historik), ej en bugg. Utan flaggan: ingen syntes, `#RES`-baserad `SIE` kvarstår.

---

## Self-Review

- **Spec-täckning:** datakontrakt (SIE_VER/ytd/amount/scenario) → Task 2; algoritm → Task 1+2; best_source → Task 3; idempotens (DELETE hela FY) → Task 2 Step 4; edge cases (brutet FY, tomma verifikat, BS-konton, periodfönster) → Task 2 Step 4–5; tester → Task 1+5; rollout → ovan. Specens `compare_coverage.sql`-ändring medvetet struken (motiverat överst).
- **Placeholder-scan:** inga TBD/TODO i kod-steg; all kod fullständig.
- **Typkonsistens:** `cumulate_ytd` returnerar `list[(str,str,float)]` — konsumeras likadant i `synthesize_sie_ver` och `check_cumulate`. `build_orgnr_lookup` → 3-tupel, uppackas som 3-tupel i `load_file`. `SOURCE_KIND_SIE_VER` används konsekvent.
- **Advisor-pass (2026-05-20):** (1) `check_coverage` gjord informativ — gatar inte exit-koden, eftersom 0 rader uppnås först efter full SE-omladdning, inte i Task 5. (2) `load_history_sie_saft.py` delar `load_file` → syntesen ärvs; dokumenterat under Kända begränsningar. (3) stale-`SIE_VER`-cleanup tillagd för bolag som börjar leverera `#PSALDO`. (4) cursor-återanvändning i `synthesize_sie_ver` (två `SELECT.fetchall()` → `DELETE` → `executemany`) sker på wrapperns delade cursor inom samma transaktion — read-your-writes gäller; verifiera vid code review mot `db.py:65-93`.
