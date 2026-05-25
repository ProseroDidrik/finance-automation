# T3 — Runbook: PII-minimering via reporting-vyer

**Status:** ✅ Live i prod 2026-05-25.
**Spec-ref:** Säkerhetsremediering, uppgift T3.

> **Säkerhetsprincip:** mcp_readonly får inte se rå PII. Läsning av
> personalinformation och journal-fritext måste gå genom `reporting.*`-vyerna
> där namn pseudonymiseras, födelsedatum grovkornas och personnummer maskas.

## Vad ändrades

| Lager | Före | Efter |
|---|---|---|
| `reporting`-schema | Fanns inte | Skapat, mcp_readonly har USAGE |
| `reporting.personnel` | – | Pseudo-id `EMP_{id}`, birth_year istället för birth_date, lön + termination_reason utelämnade |
| `reporting.journal_sie` | – | Personnummer-mönster `[PNR]`-maskat i voucher_text + transaction_text |
| `reporting.journal_saft` | – | Personnummer-mönster `[PNR]`-maskat i line_description + transaction_description |
| `public.fact_personnel` (mcp_readonly) | SELECT | REVOKE — `permission denied` om någon försöker |
| `public.fact_journal_sie` (mcp_readonly) | SELECT | REVOKE |
| `public.fact_journal_saft` (mcp_readonly) | SELECT | REVOKE |
| `etl_writer` mot PII-tabeller | DML | DML — oförändrat (loaders måste fortsatt kunna skriva) |

## ⚠️ Externa MCP-testare (Eva, Erik) — behöver meddelande

`public.fact_personnel`, `public.fact_journal_sie` och `public.fact_journal_saft`
returnerar nu `ERROR: permission denied` om de queryas via MCP. Skicka detta:

> Hej! Som ett led i säkerhetsremedieringen av finance-warehouse:
>
> - **`public.fact_personnel`** är inte längre läsbar via MCP. Använd istället
>   **`reporting.personnel`**. Skillnader: `employee_name` är ersatt med
>   `employee_ref` (`EMP_{id}`-pseudonym), `birth_date` är ersatt med `birth_year`
>   (heltal), `salary_local` och `termination_reason` är borttagna.
> - **`public.fact_journal_sie` / `_saft`** är inte längre läsbara. Använd
>   **`reporting.journal_sie` / `reporting.journal_saft`**. Skillnader: svenska
>   personnummer-mönster (`YYMMDD-NNNN` eller `YYMMDD+NNNN`) i fritextfält
>   ersätts med `[PNR]`. Alla andra kolumner är oförändrade.
>
> Kör `describe_schema` igen i en ny konversation så uppdateras tabellistan.
> Pingar du oss om en query ger oväntad `permission denied`.

## Konservativa defaults — AWAITING_DPO

Migrationen utesluter två fält per försiktighet. När juridik svarat, lägg
till dem genom att uppdatera vyn (CREATE OR REPLACE VIEW reporting.personnel):

| Fält | Default | DPO behöver svara på |
|---|---|---|
| `salary_local` | Borttaget | Behövs lönen för dataanalys? Om ja: behåll. |
| `termination_reason` | Borttaget | Är frikoppling-orsak nödvändig för HR-analys? |
| `birth_year` | Behållet | Räcker det? Eller ska bara åldersband (20-29, 30-39 osv) exponeras? |

**Inte hanterat här** (separat ticket):
- `dim_supplier_register.supplier_name` / `fact_supplier_spend.namn` — kan
  innehålla enskild firma med personnamn. T9-iteration.
- `dim_account_map.description` — fri text, sällan PII men möjligt.

## Acceptanskriterier (alla PASS, 22/22)

Verifierat 2026-05-25 via `_verify_t3.py`:

- Schema + 3 vyer finns, mcp_readonly har USAGE + SELECT
- mcp_readonly saknar SELECT på alla tre PII-tabellerna
- etl_writer behåller full DML på PII-tabellerna
- Alla 3,495 personnel-rader har `EMP_{id}`-format
- birth_year mellan 1900-2030 eller NULL
- `salary_local`, `termination_reason`, `employee_name`, `birth_date` saknas i vyn
- PNR-regex maskar både `-` och `+`-format, lämnar text utan PNR oförändrad
- 0 läckta PNR-mönster i `reporting.journal_sie` (10.4M rader) eller
  `reporting.journal_saft` (4.9M rader)

End-to-end live via deployed MCP:
- `query_sql 'SELECT COUNT(*) FROM public.fact_personnel'`
  → `ERROR: permission denied for table fact_personnel` ✅
- `query_sql 'SELECT COUNT(*) FROM reporting.personnel'`
  → 3495 rader, 69 ms ✅

## Hur det faktiskt kördes 2026-05-25

1. Migration via `_apply.py` mot admin-URL från KV (ingen psql-variabel
   behövdes — T3 har inga lösenord, bara DDL).
2. **Gotcha:** `_apply.py` kraschade vid `\echo`-utskrift med `→` (Unicode-pil)
   på Windows-konsol (cp1252). Migrationen var redan committad innan
   krashen. Fixed: `_apply.py` reconfigurerar nu stdout/stderr till UTF-8.
3. Regression: T1 verify behövde uppdateras — T1.C förväntade SELECT på
   alla 12 public-tabeller, men T3 drog medvetet in 3 PII-tabeller från
   mcp_readonly. T1.C splittad i T1.C1 (icke-PII) + T1.C2 (PII = ingen access).

## Rollback

```powershell
$env:DATABASE_URL_ADMIN = az keyvault secret show --vault-name kv-finauto-6427 `
  --name database-url --query value -o tsv
.venv\Scripts\python.exe -c @"
import os, psycopg
with psycopg.connect(os.environ['DATABASE_URL_ADMIN'], autocommit=True) as c:
    cur = c.cursor()
    # Återställ mcp_readonly:s direktaccess
    cur.execute('GRANT SELECT ON public.fact_personnel    TO mcp_readonly')
    cur.execute('GRANT SELECT ON public.fact_journal_sie  TO mcp_readonly')
    cur.execute('GRANT SELECT ON public.fact_journal_saft TO mcp_readonly')
    # Ta bort vyerna och schema
    cur.execute('DROP VIEW IF EXISTS reporting.personnel')
    cur.execute('DROP VIEW IF EXISTS reporting.journal_sie')
    cur.execute('DROP VIEW IF EXISTS reporting.journal_saft')
    cur.execute('DROP SCHEMA IF EXISTS reporting CASCADE')
"@
```

## Beroenden / nästa steg

- **T7 (rotera pgadmin)**: nu fri — MCP, ETL och PII-läsning går genom
  dedikerade roller. Inget tjänstkonto beror på pgadmin.
- **T9 (webapp)**: webappens egen connection pool går fortfarande genom
  legacy DATABASE_URL. Om DATABASE_URL pekar på readonly-URL (per
  RUNBOOK_T2-rekommendation) kör webappen som mcp_readonly och får
  permission denied vid query mot public.fact_personnel. Om webappen
  behöver visa personnel-data måste den queryas via `reporting.personnel`.
