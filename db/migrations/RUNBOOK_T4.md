# T4 — Runbook: pgaudit-loggning + Log Analytics

**Status:** ✅ Live i prod 2026-05-25.
**Spec-ref:** Säkerhetsremediering, uppgift T4.

> **Säkerhetsprincip:** alla skrivningar (DML), behörighetsändringar (ROLE) och
> schemaändringar (DDL) ska vara spårbara per `(timestamp, user, statement)`.
> Loggarna går till en Log Analytics workspace och kan queryas via KQL.

## Vad ändrades

| Komponent | Före | Efter |
|---|---|---|
| `azure.extensions` | `pg_stat_statements` | `pg_stat_statements,PGAUDIT` |
| `shared_preload_libraries` | `pg_cron,pg_stat_statements` | `pg_cron,pg_stat_statements,pgaudit` (krävde restart) |
| `pgaudit.log` | `none` | `WRITE,ROLE,DDL` |
| `log_connections` / `log_disconnections` | `on` (oförändrat) | `on` |
| Log Analytics workspace | – | `log-finauto-6427` (30 dagars retention, PerGB2018) |
| Diagnostic settings på Postgres | – | `pg-audit-to-law` → PostgreSQLLogs + PostgreSQLFlexSessions |

## Konservativ pgaudit.log-konfiguration

Specen rekommenderar `READ,WRITE,ROLE`. **Vi valde `WRITE,ROLE,DDL` istället.** Skäl:

- Standard_B1ms är en liten server (1 vCPU, 2 GB RAM). READ-audit på 574k
  fact_balances + 10M+ journal-rader genererar mycket I/O — kan påverka
  query-performance påtagligt.
- WRITE + ROLE + DDL fångar det som är svårt att spåra på annat sätt:
  vem ändrade data, vem skapade/ändrade roll, vem körde DDL.
- READ-audit kan aktiveras *riktat* mot specifika tabeller om DPO/audit
  kräver det, via `pgaudit.role`-mekanismen och `GRANT SELECT ... TO pg_audit`
  på t.ex. `fact_personnel` — då loggas bara läsningar mot just den tabellen.

Höj till `READ,WRITE,ROLE` (per spec) om/när vi vill ha full visibility:

```powershell
az postgres flexible-server parameter set -g rg-finauto-6427 -s psql-finauto-6427 `
  --name pgaudit.log --value 'READ,WRITE,ROLE'
```

(Dynamic — kräver inte restart, träder i kraft för nya anslutningar.)

## Hur det faktiskt kördes 2026-05-25

1. **Log Analytics workspace** `log-finauto-6427` (Sweden Central, PerGB2018,
   30 dagar). Microsoft.OperationalInsights-provider auto-registrerades.
2. **`az postgres flexible-server parameter set --name azure.extensions
   --value pg_stat_statements,PGAUDIT`** — dynamisk, ingen restart.
3. **`--name shared_preload_libraries --value pg_cron,pg_stat_statements,pgaudit`**
   — `isPending: true` → restart krävs.
4. **`az postgres flexible-server restart`** — ~76 sekunder. MCP/webapp/ETL
   kopplades under tiden men anslöt automatiskt vid återstart (psycopg-pools
   reconnects). Inget user-impact eftersom det var schemalagt.
5. **`CREATE EXTENSION IF NOT EXISTS pgaudit`** — version 16.0 installerad.
6. **`pgaudit.log = 'WRITE,ROLE,DDL'`** — verifierat live via
   `SELECT current_setting('pgaudit.log')`.
7. **Diagnostic setting** — initialt failade med "register Microsoft.Insights".
   Fix: `az provider register --namespace Microsoft.Insights` (~90 sek).
   Sen lyckades `az monitor diagnostic-settings create` med PostgreSQLLogs +
   PostgreSQLFlexSessions kategorier.

## Verifiera att loggar når Log Analytics

Latens för LA-ingestion är typiskt 2-5 min. För att trigga en testlogg:

```powershell
# 1. Trigga ett audit-event (CREATE ROLE testar både DDL och ROLE)
$venvPy = "C:\path\to\.venv\Scripts\python.exe"
$env:DATABASE_URL_ADMIN = az keyvault secret show --vault-name kv-finauto-6427 `
  --name database-url --query value -o tsv
& $venvPy -c @"
import os, psycopg, time
with psycopg.connect(os.environ['DATABASE_URL_ADMIN'], autocommit=True) as c:
    cur = c.cursor()
    name = f't4_smoke_{int(time.time())}'
    cur.execute(f'CREATE ROLE {name}')
    cur.execute(f'DROP ROLE {name}')
    print(f'Triggered DDL+ROLE events with role={name}')
"@

# 2. Vänta ~5 min, sen query LA via az
$law = az monitor log-analytics workspace show -g rg-finauto-6427 -n log-finauto-6427 `
  --query customerId -o tsv
az monitor log-analytics query --workspace $law --analytics-query @"
AzureDiagnostics
| where ResourceType == 'FLEXIBLESERVERS'
| where Category == 'PostgreSQLLogs'
| where Message contains 'pgaudit' or Message contains 'AUDIT'
| order by TimeGenerated desc
| take 20
"@ -o jsonc
```

För **fortlöpande monitoring** — exempel-KQL för viktiga events:

```kusto
// Alla pgaudit-events sista timmen
AzureDiagnostics
| where ResourceType == "FLEXIBLESERVERS"
| where Category == "PostgreSQLLogs"
| where Message startswith "AUDIT:"
| project TimeGenerated, Message, errorLevel_s, sqlerrcode_s
| order by TimeGenerated desc

// CREATE/DROP/ALTER ROLE-events
AzureDiagnostics
| where Message has "AUDIT:" and Message matches regex @"(CREATE|DROP|ALTER) ROLE"
| project TimeGenerated, Message

// Alla anslutningar som pgadmin (break-glass-audit efter T7)
AzureDiagnostics
| where Category == "PostgreSQLFlexSessions"
| where Message contains "user=pgadmin"
| project TimeGenerated, Message
```

## Acceptanskriterier

- ✅ `pgaudit` extension installerad och i `shared_preload_libraries`
- ✅ `pgaudit.log = 'WRITE,ROLE,DDL'` live
- ✅ `log_connections = on` (var redan på)
- ✅ Log Analytics workspace `log-finauto-6427` skapad (30d retention)
- ✅ Diagnostic setting `pg-audit-to-law` → PostgreSQLLogs + PostgreSQLFlexSessions
- ⏳ **LA-ingestion ej verifierad live** — 8 min polling utan träff på smoke-event
  (`CREATE ROLE t4_smoke_20260525121231; DROP ROLE …`). Förväntat: 15-30 min
  vanligt för helt ny LA workspace + diagnostic setting (pipeline "kallstart").
  Verifiera när som helst nästa timme/dag med KQL nedan.

### Verify LA-ingestion (kör 15-60 min efter T4-deploy)

```powershell
$law = az monitor log-analytics workspace show -g rg-finauto-6427 -n log-finauto-6427 --query customerId -o tsv

# Något över huvud taget från Postgres?
az monitor log-analytics query --workspace $law --analytics-query @"
AzureDiagnostics
| where ResourceProvider == 'MICROSOFT.DBFORPOSTGRESQL'
| summarize Count=count(), Latest=max(TimeGenerated) by Category
"@ -o table

# Specifikt vårt smoke-event från 2026-05-25 12:12
az monitor log-analytics query --workspace $law --analytics-query @"
AzureDiagnostics
| where ResourceProvider == 'MICROSOFT.DBFORPOSTGRESQL'
| where Message contains 't4_smoke_20260525121231'
| project TimeGenerated, Message
"@ -o jsonc
```

När loggarna börjar dyka upp: spara hit-tiden i denna fil så vi vet vad
faktisk ingestion-latens var första gången.

## Påverkan på övrigt

- **Inga kodändringar** krävs i webapp, MCP eller loaders. pgaudit loggar
  på server-sidan utan att klienten behöver veta.
- **Performance:** WRITE+ROLE+DDL har låg overhead. Skulle vi senare slå på
  READ blir det märkbart för aggregat-queries — överväg höjning av servern
  (Standard_B2s eller större) om READ aktiveras.
- **Kostnad:** PerGB2018 SKU på LA = ~$2.30/GB i Sweden Central. WRITE+ROLE+DDL
  loggar förväntat <100 MB/månad för vår volym. Försumbart.

## Rollback

```powershell
# Inaktivera audit-logging utan att ta bort extension/preload-bibliotek
az postgres flexible-server parameter set -g rg-finauto-6427 -s psql-finauto-6427 `
  --name pgaudit.log --value 'none'

# Eller full rollback (kräver restart):
az postgres flexible-server parameter set -g rg-finauto-6427 -s psql-finauto-6427 `
  --name shared_preload_libraries --value 'pg_cron,pg_stat_statements'
az postgres flexible-server parameter set -g rg-finauto-6427 -s psql-finauto-6427 `
  --name azure.extensions --value 'pg_stat_statements'
az postgres flexible-server restart -g rg-finauto-6427 -n psql-finauto-6427

# Och ta bort diagnostic setting + workspace
az monitor diagnostic-settings delete --name pg-audit-to-law `
  --resource (az postgres flexible-server show -g rg-finauto-6427 -n psql-finauto-6427 --query id -o tsv)
az monitor log-analytics workspace delete -g rg-finauto-6427 -n log-finauto-6427 --force --yes
```

## Beroenden / nästa steg

- **T7 (rotera pgadmin)**: efter T4 har vi nu förmåga att verifiera i LA-loggen
  att inga ströanrop som pgadmin sker INNAN vi roterar. Plan:
  1. Trigga några timmars trafik (normal användning)
  2. KQL-query: `Message contains "user=pgadmin"` i PostgreSQLFlexSessions
  3. Om bara `db.py`-init-jobb och våra manuella migrations-körningar syns → roteringssäkert
- **READ-audit kan aktiveras riktat** om DPO/audit kräver spårning av PII-läsning
  (se `pgaudit.role`-mekanismen i kommentar ovan).
- **Alert rules**: överväg KQL-alerts i LA för t.ex. "ny DROP ROLE", "FAILED
  authentication > 5/min" — separat ticket.
