# Warehouse-schema (`data/finance.duckdb`)

Star schema. Tre **dim**-tabeller (referensdata), tre **fact**-tabeller (rådata),
en **load_history** för revision. All läsning/skrivning går genom `db.py`.

## Översikt

```
                         ┌─────────────────────┐
                         │   dim_company       │ ◄────┐
                         │  company_id (PK)    │      │
                         │  name, country, …   │      │
                         └─────────────────────┘      │
                                                       │
┌──────────────┐   ┌──────────────────────┐    ┌──────┴───────┐
│  dim_period  │ ◄─┤    fact_balances     ├──► │ dim_account_ │
│ period (PK)  │   │ company_id, period,  │    │     map      │
│ year, month  │   │ account_code, amount,│    │ account_id   │
└──────────────┘   │ source_kind, …       │    │ parent_id    │
                   └──────────────────────┘    │ is_aggregated│
                              │                 └──────────────┘
                              │
                   ┌──────────┴───────────┐
                   │  fact_journal_sie    │  ◄── verifikat (opt-in)
                   │  fact_journal_saft   │
                   └──────────────────────┘

                   ┌──────────────────────┐
                   │    load_history      │  ◄── audit log per laddning
                   └──────────────────────┘
```

## Datakällor

| source_kind | period_type | Land | Källfil | Laddas av |
|---|---|---|---|---|
| `INL` | `monthly` | DK / FI / DE | `*_INL.xlsx` (process_*-output) | `load_inl.py` |
| `SIE` | `ytd` | SE | `*.SE` (SIE-export) | `load_sie.py` |
| `SIE_PSALDO` | `ytd` | SE | (samma fil, `#PSALDO`-rader) | `load_sie.py` |
| `SAFT` | `ytd` | NO | `*_SAF-T_*.xml` | `load_saft.py` |
| `MAN` | `monthly` | alla | Mercur-export / backup-txt | `load_history_excel.py` |
| `IMP` | `monthly` | FI/DK/DE/NO | Mercur-export / backup-txt | `load_history_excel.py` |
| `IMP_ADJ` | `monthly` | alla | Mercur-export | `load_history_excel.py` |
| `IB` | `monthly` | alla | `*IB 2022.xlsx` (Mercur-rapport, per 202112) | `load_ib.py` |
| `ACCOUNT_MAP` | (n/a) | (alla) | `_params/Dimensionsmedlemmar  Konto.xlsx` | `load_account_map.py` |

YTD = year-to-date (ackumulerat sedan räkenskapsårets start). SE/NO är YTD per
file-design; INL är månadsbalans (varje fil = en månads bevegelser).

## Tabeller

### `dim_company` — bolagsregister
| Kolumn | Typ | Notering |
|---|---|---|
| `company_id` | INT PK | Bolags-ID från Dotterbolagslistan kol B |
| `name` | TEXT | Friendly name (kol E) |
| `country` | TEXT | `Sweden` / `Norway` / `Finland` / `Denmark` / `Germany` / `CENTR` / `CA` |
| `currency` | TEXT | `SEK` / `NOK` / `DKK` / `EUR` (härlett från country) |
| `orgnr` | TEXT | Org/CVR/HR-nummer (kol F) |
| `domain` | TEXT | Mejl-domän för matchning (kol J) |
| `kind` | TEXT | `consolidated`-rader skippas av process-skripten |
| `updated_at` | TIMESTAMP | Senaste sync från Excel |

Synkas via `db.sync_dim_company()` (DELETE+INSERT av allt).

### `dim_period` — kalenderdimension
| Kolumn | Typ | Notering |
|---|---|---|
| `period` | TEXT PK | `YYYYMM` |
| `year`, `month`, `quarter` | INT | Härledda |
| `period_start`, `period_end` | DATE | Första/sista dagen i månaden |

Auto-utökas av `db.sync_dim_period(con, [periods])` när nya laddningar refererar
till tidigare okända perioder.

### `dim_account_map` — kontoplans-mappning
Mappar varje bolags-konto till gruppens konsoliderade kontoplan. Källa:
`_params/Dimensionsmedlemmar  Konto.xlsx`.

| Kolumn | Typ | Notering |
|---|---|---|
| `account_id` | TEXT PK | Råidentifierare. `'10_1209'` = bolag 10 / konto 1209. `'Equi'`, `'B'` = gruppkonton |
| `description` | TEXT | Svensk beskrivning |
| `description_en` | TEXT | Engelsk beskrivning |
| `is_aggregated` | BOOLEAN | TRUE för gruppkonton (300 st), FALSE för bolagskonton |
| `parent_id` | TEXT | Förälder i hierarkin (annan rads `account_id`). NULL för rotnoder |
| `source` | TEXT | Externt källangivande |
| `company_id` | INT | Härlett från prefixet i `account_id` (NULL för gruppkonton) |
| `account_code` | TEXT | Härlett från suffixet (NULL för gruppkonton) |
| `loaded_at` | TIMESTAMP | |

`(company_id, account_code)` är join-nyckeln mot `fact_balances`.
TRUNCATE+INSERT vid varje laddning — referensdata utan period-version.

**Roterande gruppkonton** (rot=parent NULL): `B` (Balans), `BUD` (Budget),
`Ej` / `Ej RR` (sentinels), `FTE`, `P&L`, `ÅRRES` (Årets Resultat).

### `fact_balances` — saldobalanser
Den centrala read-tabellen. En rad per (bolag, period, konto, källa).

| Kolumn | Typ | Notering |
|---|---|---|
| `id` | BIGINT PK | autoincrement |
| `company_id` | INT FK→`dim_company` | |
| `period` | TEXT FK→`dim_period` | `YYYYMM` |
| `period_type` | TEXT | `monthly` (INL) eller `ytd` (SIE/SAFT) |
| `account_code` | TEXT | Bolagets eget konto (zero-padded VARCHAR) |
| `account_name` | TEXT | Beskrivning från källfilen |
| `amount` | DOUBLE | Belopp i bolagets valuta |
| `currency` | TEXT | `SEK`/`NOK`/`DKK`/`EUR` |
| `statement_type` | TEXT | `IS` / `BS` / NULL |
| `source_kind` | TEXT | `INL` / `SIE` / `SIE_PSALDO` / `SAFT` / `MAN` / `IMP` / `IMP_ADJ` / `IB` |
| `source_file` | TEXT | Relativ till `base_path` (Dropbox-roten) |
| `row_index` | INT | Ordning i källfilen |
| `scenario` | TEXT | `A` (Utfall) / `B` (Budget). Default `A`. |
| `loaded_at` | TIMESTAMP | |

**Idempotens**: laddarna `DELETE … WHERE company_id=? AND period=? AND
source_kind=?` innan INSERT. Sista laddningen vinner per lane.
MAN/IMP/IMP_ADJ inkluderar även `AND scenario=?` i DELETE för att separera
utfall (A) från budget (B).

### `dim_exchange_rate` — valutakurser
| Kolumn | Typ | Notering |
|---|---|---|
| `period` | TEXT PK | `YYYYMM` |
| `currency` | TEXT PK | `NOK` / `DKK` / `EUR` |
| `rate_type` | TEXT PK | `avg` (genomsnittskurs) / `constant` (constant currency) |
| `rate` | DOUBLE | SEK per enhet utländsk valuta |
| `loaded_at` | TIMESTAMP | |

Källa: `_params/Valutakurser.xlsx`. Laddas av `load_exchange_rates.py`. Perioder: 201912–202603.

### `fact_journal_sie` — SIE-verifikat (opt-in)
Aktiveras av `load_sie.py --include-journal`. Innehåller `#VER`/`#TRANS`-rader.

| Kolumn | Typ | Notering |
|---|---|---|
| `id` | BIGINT PK | |
| `company_id`, `period` | | period från `voucher_date` |
| `series`, `voucher_number`, `voucher_date`, `voucher_text` | | Verifikat-huvud |
| `line_no` | INT | Ordning inom verifikatet |
| `account_code`, `account_name` | | |
| `amount` | DOUBLE | Positiv = debet, negativ = kredit |
| `transaction_text`, `quantity` | | Valfritt |
| `currency`, `source_file`, `loaded_at` | | |

Användning: balanskontroll på verifikatnivå (debet=kredit per voucher).

### `fact_journal_saft` — SAF-T GeneralLedgerEntries (opt-in)
Aktiveras av `load_saft.py --include-journal`.

| Kolumn | Typ | Notering |
|---|---|---|
| `id` | BIGINT PK | |
| `company_id`, `period` | | period från `transaction_date` |
| `journal_id`, `journal_description` | | Norsk SAF-T-grupp |
| `transaction_id`, `transaction_date`, `transaction_description` | | |
| `line_no`, `record_id` | | |
| `account_code` | | |
| `debit_amount`, `credit_amount` | DOUBLE | Råvärden från XML |
| `amount` | DOUBLE | `debit − credit` |
| `line_description` | | |
| `currency`, `source_file`, `loaded_at` | | |

### `load_history` — laddnings-revision
En rad per (laddat fil) ELLER per kontoplans-laddning. Används för att felsöka
"vad hände senast" och spåra warn/error-mönster.

| Kolumn | Typ | Notering |
|---|---|---|
| `id` | BIGINT PK | |
| `company_id` | INT | NULL för referens-laddningar |
| `period` | TEXT | `YYYYMM` eller `'REF'` för kontoplan |
| `source_kind` | TEXT | `INL`/`SIE`/`SAFT`/`ACCOUNT_MAP` |
| `source_file` | TEXT | |
| `rows_loaded` | INT | |
| `sum_amount` | DOUBLE | Totalsumma — för INL ska den vara ≈ 0; för YTD = årets resultat |
| `statement_type_present` | BOOLEAN | TRUE om IS/BS-flagga fanns i källan |
| `status` | TEXT | `ok` / `warn` / `error` |
| `message` | TEXT | Fritext |
| `loaded_at` | TIMESTAMP | |

## Laddningsflöde

`gui.py` "Ladda databas"-knappen, eller från CLI:

```
py db.py                                 # init schema + sync dim_company (idempotent)
py load_account_map.py                   # referensdata (TRUNCATE+INSERT)
py load_inl.py  --period YYYYMM          # FI / DK / DE
py load_sie.py  --period YYYYMM          # SE
py load_saft.py --period YYYYMM          # NO

# Historisk engångsladdning (2022–2025):
py load_exchange_rates.py                # valutakurser (dim_exchange_rate)
py load_history_sie_saft.py             # SIE/SAF-T från _history/2022–2025/
py load_history_excel.py                # MAN/IMP/IMP_ADJ från _history/
py load_ib.py                           # ingående balanser per 202112
```

Varje laddare loggar strukturerat (`[OK]`/`[WARN]`/`[ERROR]`) till stdout +
JSONL i `_logs/{period}/`.

## Vanliga queries

```sql
-- Saldobalans för ett bolag och period
SELECT account_code, account_name, amount, statement_type
FROM fact_balances
WHERE company_id = 10 AND period = '202603' AND source_kind IN ('SIE','INL','SAFT')
ORDER BY account_code;

-- Konton mappade till gruppens kontoplan
SELECT fb.company_id, fb.account_code, fb.amount, m.parent_id AS group_account
FROM fact_balances fb
LEFT JOIN dim_account_map m
  ON fb.company_id = m.company_id AND fb.account_code = m.account_code
WHERE fb.period = '202603';

-- Vilka bolag har laddat data för en period?
SELECT period, COUNT(DISTINCT company_id) AS bolag
FROM fact_balances
GROUP BY period
ORDER BY period;

-- Senaste laddning per (bolag, period, källa)
SELECT company_id, period, source_kind, status, message, loaded_at
FROM load_history
ORDER BY loaded_at DESC
LIMIT 20;

-- Hierarkisk rollup (alla föräldrar för ett konto)
WITH RECURSIVE walk AS (
  SELECT account_id, parent_id, 0 AS depth, account_id AS leaf
  FROM dim_account_map
  WHERE account_id = '10_1209'
  UNION ALL
  SELECT m.account_id, m.parent_id, w.depth + 1, w.leaf
  FROM dim_account_map m
  JOIN walk w ON m.account_id = w.parent_id
)
SELECT * FROM walk ORDER BY depth;
```

## Volym (snapshot 2026-05-05, efter historisk inläsning)

| Tabell | Rader |
|---|---:|
| `dim_company` | 147 |
| `dim_period` | 80+ |
| `dim_account_map` | 80 729 |
| `dim_exchange_rate` | 456 |
| `fact_balances` | ~410 000 |
| `fact_journal_sie` | 600 868 |
| `fact_journal_saft` | 288 708 |
| `load_history` | 1 200+ |

Bolag per land: SE 61 · NO 42 · FI 21 · CENTR 8 · DK 8 · DE 5 · CA 2.

Perioder med data: 202112 (IB, 75 bolag) · 202201–202512 (historik) · 202601–202604 (löpande).

`fact_balances` source_kind-fördelning:
| source_kind | scenario | Rader | Perioder |
|---|---|---:|---|
| `IB` | A | 2 726 | 202112 |
| `IMP` | A | 187 122 | 202201–202512 |
| `IMP_ADJ` | A | 209 | 202212–202603 |
| `INL` | A | 6 988 | 202601–202603 |
| `MAN` | A | 5 893 | 202201–202612 |
| `MAN` | B | 45 844 | 202201–202612 |
| `SAFT` | A | 67 486 | 202212–202603 |
| `SIE` | A | 44 161 | 202212–202604 |
| `SIE_PSALDO` | A | 49 389 | 202201–202604 |

## Inspektera live

```
py -c "import duckdb; con=duckdb.connect('data/finance.duckdb', read_only=True); print(con.execute('DESCRIBE').df().to_string())"
```

eller via duckdb CLI:
```
duckdb data/finance.duckdb
> DESCRIBE;
> SELECT * FROM dim_company LIMIT 5;
```
