# db/migrations

Versionerade SQL-migrationer mot Azure Postgres (`finance`-databasen). Det här
är säkerhets- och DDL-ändringar som *inte* hör hemma i `db.py`'s `init_schema()`
(som är idempotent skapande av tabeller/index för bootstrap av en tom databas).

## Filnamnskonvention

```
YYYYMMDD_<kort_slug>.sql           — själva migrationen, idempotent
YYYYMMDD_<kort_slug>.verify.sql    — (valfritt) verifierar acceptanskriterier
```

Datum är *körningsdatum* (när migrationen skrevs), inte semantisk version. Slug
är kort kebab/snake_case som beskriver vad migrationen gör.

## Konventioner

- **Idempotent**: varje migration ska kunna köras flera gånger utan fel.
  Använd `IF NOT EXISTS`, `DO $$ ... pg_roles ... $$`, `ON CONFLICT DO NOTHING` etc.
- **Top-level statements för psql-variabler**: psql-variabler (`:'foo'`) inom
  `DO $$ ... $$`-block är opålitliga. Splittra: `DO`-blocket gör strukturen,
  ett separat `ALTER ROLE … :'pw'` sätter värdet.
- **Ingen schema-drift mot `db.py`**: tabeller skapas i `db.py`. Migrationer
  här rör roller, rättigheter, vyer, indexes och engångs-data-fix.
- **Commit-message**: `chore(db): T<n> — <slug>` så det går att spåra mot
  remediation-spec (T1–T9).

## Körning

Vi har INTE psql installerat lokalt — använd `_apply.py` (psycopg-baserad runner,
hanterar `\set`, `\echo`, `:'var'`-substitution så .sql-filerna också går att
köra med psql om det installeras senare).

```powershell
# Admin-URL från Key Vault, lösenord skickas via --var (aldrig i fil/argv-historik).
$env:DATABASE_URL_ADMIN = az keyvault secret show --vault-name kv-finauto-6427 `
  --name database-url --query value -o tsv

C:\path\to\.venv\Scripts\python.exe db\migrations\_apply.py `
  db\migrations\<file>.sql --var <varname>=<value>
```

Alternativ med psql om det installeras:

```powershell
psql "$env:DATABASE_URL_ADMIN" -v ON_ERROR_STOP=1 `
  -v mcp_pw="<lösenord>" -f db/migrations/<file>.sql
```

Verifiera sedan via `verify.sql` (psql) eller skriv en Python-verifyfil med
strukturerad PASS/FAIL-output (se commit-historik för T1-exempel).

## Migrationslogg

| Datum | Migration | T# | Kort | Status |
|---|---|---|---|---|
| 2026-05-25 | `20260525_mcp_readonly_role.sql` | T1 | Read-only-roll för MCP | ✅ live i prod (verifierad via deployed MCP, current_user=mcp_readonly) |
| 2026-05-25 | `20260525_etl_writer_role.sql`   | T2 | DML-roll för ETL-loaders (ingen DDL) | ✅ live i prod + db.py fail-fast aktiv (loaders kräver DATABASE_URL_ETL — se RUNBOOK_T2.md) |
| 2026-05-25 | `20260525_reporting_views.sql`   | T3 | PII-vyer (reporting.*) + REVOKE mcp_readonly på PII-råtabeller | ✅ live i prod, e2e via deployed MCP (PII → permission denied, reporting.personnel → 3495 rader, 69 ms). Konservativa defaults; salary_local/termination_reason AWAITING_DPO. Externa MCP-testare måste byta till reporting.* — se RUNBOOK_T3.md för meddelande |
| 2026-05-25 | _(ingen migration — kod & appsettings)_ | T9 | Webapp på mcp_readonly + reporting.* + SQL/auth-audit | ✅ live i prod, /api/health 200 OK. /api/personnel/employees ändrad API-form (employee_ref + birth_year; salary/termination borttagna). Frontend personnel-fliken behöver UI-anpassning — se RUNBOOK_T9.md |
