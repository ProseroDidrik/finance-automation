# T1 — Runbook: read-only-roll för warehouse-MCP

**Status:** Kod klar (denna PR), pending DevOps-körning.
**Skapad:** 2026-05-25
**Spec-ref:** Säkerhetsremediering, uppgift T1.

> **Säkerhetsprincip:** ingen runtime-komponent (MCP, ETL, webapp) ska ansluta
> som `pgadmin`/`azure_pg_admin`. Adminkontot är "break-glass" — manuell
> administration, aldrig tjänster.

## Vad som ändras

| Lager | Före | Efter |
|---|---|---|
| MCP DB-roll | `pgadmin` (full superuser-ish) | `mcp_readonly` (SELECT-only, `default_transaction_read_only=on`) |
| Skrivskydd | Regex-filter i `query_sql` (lager 1) | DB-roll + regex-filter (lager 2) |
| Key Vault-secret | `database-url` (admin) | `database-url-readonly` (ny) + befintlig |
| `mcp_server.py` | Läser `database-url` från KV | Läser `database-url-readonly` |

## Steg

### 1. Generera ett starkt lösenord (lokalt, DevOps)

Lägg det aldrig i filer — pipea direkt in i psql och Key Vault.

**Använd CSPRNG**, inte `Get-Random` (det är PRNG seedad från klockan — fel
primitiv för DB-credentials).

```powershell
# 30 bytes → ~40 tecken base64 (url-safe-trimmat)
$bytes = New-Object byte[] 30
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$mcpPw = ([Convert]::ToBase64String($bytes) -replace '[+/=]', '')

$mcpPw | Set-Clipboard
$hash = (Get-FileHash -InputStream ([IO.MemoryStream]::new([Text.Encoding]::UTF8.GetBytes($mcpPw))) -Algorithm SHA256).Hash.Substring(0,12)
"[ok] Genererat lösenord ($($mcpPw.Length) tecken, CSPRNG). SHA256-hash[:12]: $hash"
```

### 2. Kör migrationen som `pgadmin`

```powershell
# DATABASE_URL_ADMIN ska vara admin-strängen (samma som hittills använts).
# Hämta den från Key Vault `database-url` om du inte har den i shell-env.
$adminUrl = az keyvault secret show --vault-name kv-finauto-6427 --name database-url --query value -o tsv
$env:DATABASE_URL_ADMIN = $adminUrl

# Lösenordet skickas via -v, inte i filen.
psql $env:DATABASE_URL_ADMIN -v ON_ERROR_STOP=1 `
  -v mcp_pw=$mcpPw `
  -f db/migrations/20260525_mcp_readonly_role.sql
```

Förväntat: `[OK] T1 — mcp_readonly migration applied.`

### 3. Verifiera acceptanskriterierna (admin-sidan)

```powershell
psql $env:DATABASE_URL_ADMIN -v ON_ERROR_STOP=1 `
  -f db/migrations/20260525_mcp_readonly_role.verify.sql
```

Checka mot kommentarerna i filen — alla `can_select=t`, alla
`can_insert/update/delete=f`, `is_admin_member=f`, `can_create_in_public=f`.

### 4. Bygg connection string för MCP-rollen

```powershell
# Format: postgresql://mcp_readonly:<pw>@<host>:5432/finance?sslmode=require
$pgHost = "psql-finauto-6427.postgres.database.azure.com"
# URL-encoda lösenordet — [uri]::EscapeDataString är portabelt (PS 5.1 + Core).
$pwEnc = [uri]::EscapeDataString($mcpPw)
$mcpUrl = "postgresql://mcp_readonly:$pwEnc@$pgHost:5432/finance?sslmode=require"
```

### 5. Manuell smoke-test som `mcp_readonly` (innan vi byter MCP:n)

```powershell
psql $mcpUrl -c "SELECT current_user, current_setting('default_transaction_read_only');"
# Förväntat: mcp_readonly | on

psql $mcpUrl -c "SELECT COUNT(*) FROM fact_balances;"
# Förväntat: ett tal (~hundra tusen)

# Ska FAILA — skrivskydd på rollnivå:
psql $mcpUrl -c "INSERT INTO load_history (status, loaded_at) VALUES ('test', now());"
# Förväntat fel: ERROR:  cannot execute INSERT in a read-only transaction
```

### 6. Lägg connection string i Key Vault

```powershell
az keyvault secret set `
  --vault-name kv-finauto-6427 `
  --name database-url-readonly `
  --value $mcpUrl `
  --tags purpose=mcp_readonly created=2026-05-25 task=T1
```

Rensa variabler ur shell:
```powershell
Remove-Variable mcpPw, pwEnc, mcpUrl, adminUrl -ErrorAction SilentlyContinue
```

### 7. Peka om MCP-servern

`mcp_server.py:101-115` läser `DATABASE_URL`-env FÖRST, och faller bara
tillbaka på Key Vault om env är tom:

```python
url = os.environ.get("DATABASE_URL")
if url:
    return url
# ... bara nu konsulteras KV
```

App Service har nästan säkert `DATABASE_URL` satt som en application setting
med en KV-referens (`@Microsoft.KeyVault(SecretUri=…/database-url/…)`). Då
NÅS aldrig `AZURE_KEYVAULT_SECRET`-fallbacken. Att sätta den variabeln gör
ingenting — kopplingen fortsätter komma från `pgadmin` och det syns inte i
loggen. Vi måste uppdatera `DATABASE_URL` själv.

**Steg 7a — verifiera vad som faktiskt är satt:**

```powershell
$RG  = "<resursgrupp>"
$APP = "<app-service-namn>"

az webapp config appsettings list -g $RG -n $APP `
  --query "[?name=='DATABASE_URL' || name=='AZURE_KEYVAULT_SECRET']" -o jsonc
```

Tre möjliga utfall, hantera enligt nedan:

**Fall A — `DATABASE_URL` är en KV-referens** (vanligast):
Värdet ser ut som `@Microsoft.KeyVault(SecretUri=https://kv-finauto-6427.vault.azure.net/secrets/database-url/)`.
Byt SecretUri till nya secret:

```powershell
az webapp config appsettings set -g $RG -n $APP --settings `
  "DATABASE_URL=@Microsoft.KeyVault(SecretUri=https://kv-finauto-6427.vault.azure.net/secrets/database-url-readonly/)"
```

**Fall B — `DATABASE_URL` är en klartext-sträng (admin-credential):**
Det är i sig en finding — credentials ska vara KV-refererade. Lös samtidigt:
ta bort env-värdet och låt KV-fallbacken sköta det.

```powershell
az webapp config appsettings delete -g $RG -n $APP --setting-names DATABASE_URL
az webapp config appsettings set    -g $RG -n $APP --settings AZURE_KEYVAULT_SECRET=database-url-readonly
```

**Fall C — `DATABASE_URL` saknas (ovanligt, men möjligt för MCP-appen om den
bara kör mot KV):** sätt `AZURE_KEYVAULT_SECRET=database-url-readonly`.

App Service restartar automatiskt vid appsettings-ändring.

**Steg 7b — permanent kodfix (separat PR, ej blockerande):**
Ändra default i `mcp_server.py:48` från `"database-url"` till
`"database-url-readonly"` så lokal körning utan env-override också pekar
rätt. Tas i en T1.b-commit efter att DevOps verifierat steg 8.

### 8. End-to-end-acceptans via MCP — KÖR DENNA FÖRST

**Innan något annat verifieras**: kör en MCP-fråga från Claude Code/Desktop:

```sql
SELECT current_user, current_setting('default_transaction_read_only');
-- Förväntat: mcp_readonly | on
-- Om resultatet är pgadmin → step 7 har INTE tagit. Se "Felsökning" nedan.
```

Är det grönt → fortsätt med skrivskydds-check:

Filtret i `query_sql` (`mcp_server.py:57-61`) blockerar redan
INSERT/UPDATE/DELETE-keywords på textnivå, så en MCP-INSERT fångas av
textfiltret innan den når DB:n. **För att verifiera att DB-lagret
verkligen tar över**, kör direkt mot connection-strängen från steg 5 (förbi
MCP) — om INSERT faiar med `cannot execute INSERT in a read-only transaction`
är båda lagren aktiva.

#### Felsökning om steg 8 visar pgadmin

Då är `DATABASE_URL` i App Service troligen fortfarande pekande mot admin-secret.
Kör igen:
```powershell
az webapp config appsettings list -g $RG -n $APP --query "[?name=='DATABASE_URL']" -o jsonc
```
och bekräfta att SecretUri slutar med `/secrets/database-url-readonly/`.

App Service cache:ar KV-referenser i upp till 24 h men app-restart triggar
re-resolve. Om värdet ser rätt ut men `current_user` fortfarande är pgadmin:
```powershell
az webapp restart -g $RG -n $APP
```

## Rollback

Om något går sönder och MCP behöver tillbaka till admin temporärt:

```powershell
# Återställ DATABASE_URL till admin-secret (snabbast)
az webapp config appsettings set -g $RG -n $APP --settings `
  "DATABASE_URL=@Microsoft.KeyVault(SecretUri=https://kv-finauto-6427.vault.azure.net/secrets/database-url/)"

# Rollen kan ligga kvar i DB:n — den är harmlös tills någon ansluter med den.
# Vill du städa helt:
psql $env:DATABASE_URL_ADMIN -c "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM mcp_readonly; REVOKE ALL ON SCHEMA public FROM mcp_readonly; REVOKE ALL ON DATABASE finance FROM mcp_readonly; DROP ROLE mcp_readonly;"

# Och ta bort KV-secret:
az keyvault secret delete --vault-name kv-finauto-6427 --name database-url-readonly
```

## Beroenden / blockeringar för T2

T2 (etl_writer) följer samma mönster och bör köras direkt efter att T1 är grön.
T7 (rotera pgadmin) bör vänta tills T1+T2 är klara — annars låser vi ute MCP/ETL.

---

## Hur T1 faktiskt kördes 2026-05-25 (live-spår)

Runbook ovan beskriver psql-vägen. I praktiken kördes T1 utan psql installerat
— använd den här sektionen som facit vid nästa rotation.

**Verktyg:** `az` 2.86.0 + `.venv\Scripts\python.exe` (psycopg 3.3.4). Inget psql.

**Sekvens som faktiskt fungerade:**

1. **Admin-URL från KV:** `az keyvault secret show --vault-name kv-finauto-6427
   --name database-url --query value -o tsv` → `$env:DATABASE_URL_ADMIN`.

2. **Lösenord:** CSPRNG via `[System.Security.Cryptography.RandomNumberGenerator]`,
   40 tecken base64-trimmat.

3. **Migration:**
   `.venv\Scripts\python.exe db\migrations\_apply.py
   db\migrations\20260525_mcp_readonly_role.sql --var mcp_pw=$mcpPw`
   Returnerade två postgres-notices ("no privileges were granted for
   pg_stat_statements{_info}") — förväntat, det är extension-vyer som pgadmin
   inte äger.

4. **Verifiering:** `_verify_t1.py` — 13/13 PASS.

5. **Smoke-test som mcp_readonly:** Python inline, läste 574 063 rows från
   `fact_balances`, INSERT blockerades med `psycopg.errors.ReadOnlySqlTransaction`.

6. **KV-secret:** `az keyvault secret set --vault-name kv-finauto-6427
   --name database-url-readonly --value $mcpUrl --tags purpose=mcp_readonly
   task=T1`. Read-back-verifierad genom att ansluta med värdet från KV.

7. **App Service appsetting:** Här krånglade `az webapp config appsettings set`:
   - `--settings DATABASE_URL=@Microsoft.KeyVault(...)` → `az` tolkar `@`
     som "läs från fil", värdet blev `null`.
   - `--settings @file.json` med array-format → `az` accepterade men returnerade
     value=null och förändrade inte appsetting (ingen synlig fel).
   - `--settings-file` → finns inte i `az webapp config appsettings set`.

   **Fungerande approach: `az rest` PUT mot ARM-endpoint:**
   ```powershell
   $sub = az account show --query id -o tsv
   $base = "https://management.azure.com/subscriptions/$sub/resourceGroups/$RG/" +
           "providers/Microsoft.Web/sites/$APP/config/appsettings"
   # OBS: POST på /list (Azures quirk), inte GET
   $current = az rest --method post --url "$base/list?api-version=2022-03-01" | ConvertFrom-Json
   # Bygg properties-dict — undvik $current.properties.PSObject.Properties
   # (det flätar in name/value/slotSetting-skräp). Använd ConvertFrom-Json -AsHashtable
   # eller hårdkoda nycklarna.
   ...
   az rest --method put --url "$base?api-version=2022-03-01" --body "@$tmpBody"
   ```

   **Fälla som biten oss:** PowerShell:s `PSObject.Properties`-iteration över
   `$current.properties` lade till `name`, `value`, `slotSetting` som extra
   appsettings — städades med `az webapp config appsettings delete
   --setting-names name slotSetting value`.

8. **End-to-end-verify:** HTTPS POST till
   `https://app-finauto-mcp-6427.azurewebsites.net/mcp` med MCP streamable-http
   JSON-RPC (initialize → notifications/initialized → tools/call query_sql med
   `SELECT current_user, current_setting('default_transaction_read_only')`).
   Svar: `mcp_readonly | on`. T1 ✅.
