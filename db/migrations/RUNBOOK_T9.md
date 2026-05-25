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

## Advisor-fångade follow-ups (samma session, samma PR)

Efter T9-commit:n körde advisor genom och fångade 5 saker. Tre fixade inline:

### T3.c — Column-level grants på journal-tabellerna

compare_coverage timar ut (>30s) när den queryar `reporting.journal_sie/saft`
eftersom regex-maskningen evalueras per rad även när vi bara summerar `amount`.
Lösning: ny migration `20260525_journal_column_grants.sql` ger mcp_readonly
SELECT på alla kolumner UTOM voucher_text/transaction_text/line_description.
Webapp coverage-SQL pekas tillbaka till public.fact_journal_* (snabbare, säkert
eftersom fritext fortfarande är kolumn-blockerad). 9/9 column-grants verifierade.

### T9-fu — Höj mcp_readonly timeout 30s → 60s

Även med column-grants tog compare_coverage 4 månader 29-39s. Migration
`20260525_mcp_readonly_timeout_60s.sql` höjer mcp_readonly:s default
statement_timeout till 60s. Påverkar både MCP och webapp. Acceptabel paus
för Claude-konversation.

### T9.b — KV-refera Easy Auth-secret

`MICROSOFT_PROVIDER_AUTHENTICATION_SECRET` låg som klartext-appsetting på
App Service (för Easy Auth-providern). Skapad ny KV-secret `easyauth-provider-secret`
och uppdaterat appsetting till `@Microsoft.KeyVault(...)`-ref. Webapp restartad,
/api/health 200 OK.

### Bucket-cap i report_pivot (DoS-prevention)

`_MAX_BUCKETS = 60` i main.py. Orimligt långa period-range (t.ex. 50 år
månadsvis) skulle annars generera tusentals SQL-placeholders.

## Inte fixat (DPO-fråga + framtida ärende)

### Pseudonymen `EMP_{id}` är trivialt avpseudonymiserbar

`reporting.personnel.employee_ref = 'EMP_' || id::text` är 1:1-mappad mot
fact_personnel:s auto-increment-id. Vem som helst med access till BÅDA
`public.fact_personnel` (admin, etl_writer) och `reporting.personnel`
(mcp_readonly/webapp) kan trivialt JOIN:a på `id` och avpseudonymisera allt.
Det är inte "true pseudonymisering" — fungerar bara som access-gate.

**Tillägg till DPO-frågorna från T3:** "Räcker access-gate-pseudonymisering,
eller behöver vi byta surrogat-id till ett random UUID som inte mappas till
radens id?" Om det senare: kräver ny kolumn i fact_personnel + ett mapping-
register som bara HR-roll ser. Större ändring.

## Beroenden / nästa steg

- **T7 (rotera pgadmin)**: NU helt fri. Inga tjänster (MCP, ETL, webapp) beror på
  admin-credential i prod. Lokal dev kräver `DATABASE_URL_ADMIN` temporärt för
  `py db.py` — det fortsätter fungera efter rotation om man uppdaterar lokalt.
  Förbered T7 med grep efter `database-url` (admin-namnet) i hela repot +
  audit av Key Vault Secrets User-principals på `kv-finauto-6427/database-url`.
- **Frontend personnel-detail**: anpassa till nya API-fälten (`employee_ref`,
  `birth_year`, inga salary/termination).
- **Compare_coverage optimering**: 39s första körningen är gränsfall. Lägga
  till ett (period, account_code)-index på fact_journal_saft skulle hjälpa.
  Backlog.
