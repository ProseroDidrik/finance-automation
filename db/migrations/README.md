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

Migrationer körs manuellt av en med admin-rättigheter (`pgadmin` eller en
Entra-grupp med owner-rättigheter). Lokalt:

```powershell
# Hemligheten skickas via -v, inte i filen.
psql "$env:DATABASE_URL_ADMIN" -v ON_ERROR_STOP=1 `
  -v mcp_pw="<lösenord från Key Vault>" `
  -f db/migrations/20260525_mcp_readonly_role.sql
```

Verifiera sedan:

```powershell
psql "$env:DATABASE_URL_ADMIN" -v ON_ERROR_STOP=1 `
  -f db/migrations/20260525_mcp_readonly_role.verify.sql
```

## Migrationslogg

| Datum | Migration | T# | Kort | Status |
|---|---|---|---|---|
| 2026-05-25 | `20260525_mcp_readonly_role.sql` | T1 | Read-only-roll för MCP | ⏳ pending DevOps |
