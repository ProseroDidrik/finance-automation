# T9 — Runbook: webapp SQL-säkerhet + DB-roll-minimering

**Status:** ✅ Live i prod 2026-05-25.
**Spec-ref:** Säkerhetsremediering, uppgift T9.

> **Säkerhetsprincip:** webappen ska inte ansluta som `pgadmin` och ska
> följa samma PII-minimering som MCP — dvs läsning av persondata via
> `reporting.*`-vyer, aldrig direktaccess till `public.fact_personnel`.

## Vad ändrades

| Lager | Före | Efter |
|---|---|---|
| Webapp DB-roll | `pgadmin` (via legacy DATABASE_URL) | `mcp_readonly` (delar T1:s roll) |
| Webapp KV-secret | `database-url` (admin) | `database-url-readonly` (T1) |
| App Service `DATABASE_URL` | KV-ref till `database-url` | KV-ref till `database-url-readonly` |
| `/api/personnel/countries` | `FROM fact_personnel` | `FROM reporting.personnel` |
| `/api/personnel/summary` | `FROM fact_personnel` + `personnel_summary.sql` | `FROM reporting.personnel` |
| `/api/personnel/employees` | `employee_name`, `birth_date`, `salary_local`, `termination_reason` | `employee_ref`, `birth_year`; salary/termination borttagna |
| `compare_coverage.sql` | `FROM fact_journal_sie/saft` | `FROM reporting.journal_sie/saft` |
| `coverage_accounts.sql` | `FROM fact_journal_sie/saft` | `FROM reporting.journal_sie/saft` |

**Inga nya DB-roller skapade.** Webappen delar `mcp_readonly` med MCP-servern
— rättighetsmässigt identiska behov (read-only + reporting-vyer). Om vi
senare vill separera (för audit-loggning per komponent) är det en T9.b.

## SQL-injection-audit (samtidig granskning)

Genomgång av alla 17 endpoints + 7 SQL-filer i `webapp/backend/`:

- Alla `con.execute(sql, [params])`/`fetch_dicts(sql, [params])` använder
  psycopg-parametrar (`%s`-placeholders, inte string-formatting).
- F-strings i koden är **inte i SQL**: cache-keys (`f"periods:{company_id}"`),
  felmeddelanden (`detail=f"Bolag {id} hittades inte"`), eller error-text.
- En icke-parameteriserad path värd att notera: `report_pivot`-endpointet
  bygger `bucket_values_clause = "VALUES " + ", ".join(["(%s, %s, %s)"] * N)`
  via `sql_template.replace("{bucket_values}", ...)`. Antalet placeholders
  styrs av `len(buckets)` (heltal, härlett från valideringen period_from/to),
  inte av användarsträng. **Säkert.**
- Coverage-SQL har `'@period_lo@' AND period <= '@period_hi@'` — main.py
  substituerar med `.replace("@period_lo@", ...)` mot validerade YYYYMM-strängar
  (regex `len(period)==6 and period.isdigit()`). Inputvalidering finns men
  string-substitution istället för parameterbinding är en svaghet — refactor
  till `%s` rekommenderas men inte blockerande (input är hård-validerad).

## Auth-audit

`webapp/backend/auth.py`:
- Easy Auth gate via `X-MS-CLIENT-PRINCIPAL`-header (Entra ID)
- Maestro-grupp tvingad via `MAESTRO_GROUP_ID` (env)
- `/api/health` exempt (App Service liveness-probe)
- `DEV_AUTH_BYPASS=1` vägrar slå på sig om `WEBSITE_SITE_NAME` är satt
  (App Service-detektering) — fail-closed i prod
- `MAESTRO_GROUP_ID` saknad → alla requests faller på authorization (fail-closed)

**Sidofinding** (utöver T9-spec): `MICROSOFT_PROVIDER_AUTHENTICATION_SECRET`
ligger som klartext-appsetting på App Service (för Easy Auth-providern).
Bör KV-refereras likt övriga secrets — separat T9.c-ticket.

## API-brott

`/api/personnel/employees` ändrar svarets struktur:

```diff
- "employee_name":      "Anna Andersson",
+ "employee_ref":       "EMP_8811",
- "birth_date":         "1989-05-12",
+ "birth_year":         1989,
- "termination_reason": "Egen begäran",
- "salary_local":       42000,
```

Frontenden på personnel-detalj-fliken behöver anpassas. UI-fix är **separat
ticket** — inte blockerande för T9-säkerhetsleveransen.

## Hur det faktiskt kördes 2026-05-25

1. **SQL-filer:** byt 4 ställen i compare_coverage.sql + coverage_accounts.sql +
   personnel_summary.sql till `reporting.*`-vyer.
2. **main.py:** 3 personnel-endpoints uppdaterade.
3. **Smoke (lokalt med RO_URL):** alla personnel-queries returnerar data,
   sample `('EMP_8811', 'Lukkoseppä', 1989, 2024-01-10)`.
4. **App Service appsettings:**
   - GET via `az rest POST /list?api-version=2022-03-01` (Azures quirk —
     POST på "list"-endpoint).
   - **PowerShell PSObject-fälla från T1 fångad denna gång:** iteration via
     `$current.properties.PSObject.Properties | Get-Member -MemberType NoteProperty`
     plockar bara data-properties, inte interna name/value/slotSetting.
     Resultat: 8 settings → 8 settings, ingen pollution.
   - PUT med ConvertTo-Json → tempfil → `az rest --body "@$tmpfile"`.
5. **Verify:** webapp /api/health svarar 200 OK efter ~10s restart, kör
   mot mcp_readonly utan errors.

## Acceptanskriterier

- ✅ Ingen icke-parametriserad SQL med användardata i webapp
- ✅ Auth gating: Easy Auth + Maestro-grupp, fail-closed
- ✅ Webapp ansluter som mcp_readonly, INTE som pgadmin
- ✅ Personnel-endpoints går via reporting.personnel
- ✅ Coverage-SQL går via reporting.journal_sie/saft
- ✅ /api/health 200 OK live efter omkoppling

## Rollback

```powershell
# Återställ webapp till admin-credential (säkerhetsregression):
$sub = az account show --query id -o tsv
$RG = "rg-finauto-6427"; $APP = "app-finauto-6427"; $KV = "kv-finauto-6427"
$base = "https://management.azure.com/subscriptions/$sub/resourceGroups/$RG/providers/Microsoft.Web/sites/$APP/config/appsettings"
$current = az rest --method post --url "$base/list?api-version=2022-03-01" | ConvertFrom-Json
$props = @{}
$current.properties | Get-Member -MemberType NoteProperty | ForEach-Object {
  $props[$_.Name] = $current.properties.($_.Name)
}
$props['DATABASE_URL'] = "@Microsoft.KeyVault(SecretUri=https://$KV.vault.azure.net/secrets/database-url/)"
$body = @{ properties = $props } | ConvertTo-Json -Depth 10 -Compress
$tmp = New-TemporaryFile
[System.IO.File]::WriteAllText($tmp, $body, [System.Text.UTF8Encoding]::new($false))
az rest --method put --url "$base`?api-version=2022-03-01" --body "@$tmp"
Remove-Item $tmp
```

Och: revert SQL-filerna i git.

## Beroenden / nästa steg

- **T7 (rotera pgadmin)**: NU helt fri. Inga tjänster (MCP, ETL, webapp) beror på
  admin-credential i prod. Lokal dev kräver `DATABASE_URL_ADMIN` temporärt för
  `py db.py` — det fortsätter fungera efter rotation om man uppdaterar lokalt.
- **T9.b** (separera webapp_reader från mcp_readonly): om audit-loggning per
  komponent blir behov.
- **T9.c** (KV-refera `MICROSOFT_PROVIDER_AUTHENTICATION_SECRET`): separat,
  liten ändring.
- **Frontend personnel-detail**: anpassa till nya API-fälten (`employee_ref`,
  `birth_year`, inga salary/termination).
