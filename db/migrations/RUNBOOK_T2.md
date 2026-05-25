# T2 — Runbook: skrivande ETL-roll `etl_writer`

**Status:** ✅ Live i prod 2026-05-25 (DB-rollen + KV-secret skapade,
db.py uppdaterad med fail-fast).
**Spec-ref:** Säkerhetsremediering, uppgift T2.

> **Säkerhetsprincip:** loaders får inte köra som `pgadmin`. En bugg i en
> loader som admin kan TRUNCATE/DELETE vad som helst. `etl_writer` har DML
> men ingen DDL — schemaändringar reserveras för admin.

## Vad ändrades

| Lager | Före | Efter |
|---|---|---|
| ETL DB-roll | `pgadmin` (rolbypassrls, full DDL/DML) | `etl_writer` (DML only, INGEN DDL) |
| ETL connection-skydd | Inget | `db.connect()` fail-fast om current_user är admin |
| KV-secret | `database-url` (admin) | `database-url-etl` (ny) |
| `db.py:connect()` | `connect(read_only=False)` | `connect(read_only=False, role='etl')` |
| `db.py:main()` | `connect()` (admin via DATABASE_URL) | `connect(role='admin')` (explicit) |

## Vad du behöver göra för att loaders ska fungera lokalt

Skrivande loaders (`connect()`) kräver nu `DATABASE_URL_ETL` — utan den
faller anslutningen tillbaka på `DATABASE_URL`, ser admin, **avbryter med
RuntimeError**. Read-only-script (`connect(read_only=True)`) påverkas INTE
— de undantas från fail-fast eftersom de inte kan skada data.

### ⚠️ Känd issue efter T3: webapp `/api/personnel/*` failar lokalt om
`DATABASE_URL=readonly`

Efter T3 (PII-vyer) saknar `mcp_readonly` SELECT på `public.fact_personnel`.
Webapp-poolen läser fortfarande `public.fact_personnel` i
`webapp/backend/main.py:12-14`-endpointsen — de returnerar 500 lokalt om
poolen är ansluten som mcp_readonly. **Två workarounds tills T9:**

1. Kör webapp tillfälligt med admin-URL: `$env:DATABASE_URL = (az ... database-url)`
   innan `uvicorn webapp.backend.main:app` — bryter T1.b lokalt men `/api/personnel/*` funkar.
2. Uppdatera webapp att queryra `reporting.personnel` istället (egentligen T9-scope
   men kan göras opportunistiskt om du behöver webappen lokalt nu).

### ⚠️ Viktigt: undvik `DATABASE_URL=admin` lokalt

Lokal MCP (`.mcp.json` startar `mcp_server.py`) ärver shell-env. Om
`DATABASE_URL` är satt och pekar på admin-secret, **nullifieras T1.b**
(mcp_server.py:48 går aldrig till KV-fallbacken som T1.b ändrade default
för). Lokal MCP fortsätter köra som pgadmin trots T1+T1.b.

**Rekommenderad setup** (`$PROFILE`):

```powershell
# DATABASE_URL pekar på READ-ONLY-secret. Det matchar:
#  - Lokal MCP (via .mcp.json) — kör som mcp_readonly ✅
#  - Webapp (via db.database_url() → ConnectionPool) — bara SELECT:ar,
#    så mcp_readonly räcker tills T9 ger den egen roll.
$env:DATABASE_URL = (az keyvault secret show --vault-name kv-finauto-6427 `
                       --name database-url-readonly --query value -o tsv)

# DATABASE_URL_ETL — bara loaders ser denna. Skrivande etl_writer.
$env:DATABASE_URL_ETL = (az keyvault secret show --vault-name kv-finauto-6427 `
                          --name database-url-etl --query value -o tsv)

# DATABASE_URL_ADMIN — break-glass för db.py:main() / schema-init.
# Sätt bara temporärt när du faktiskt kör `py db.py`.
# Inte i $PROFILE — håll admin-credential ute ur default shell-env.
```

### Verifiera

```powershell
# Loader-flöde (skrivande)
py -c "import db; print(db.connect().execute('SELECT current_user').fetchone())"
# Förväntat: ('etl_writer',)

# Read-only-script (suggest_categories, check_*, verify_*) — funkar oavsett URL
py -c "import db; print(db.connect(read_only=True).execute('SELECT current_user').fetchone())"
# Förväntat (med setup ovan): ('mcp_readonly',)  — läs-script går genom readonly nu

# Admin-väg (för db.py:main eller engångs-DDL)
$env:DATABASE_URL_ADMIN = (az keyvault secret show --vault-name kv-finauto-6427 `
                            --name database-url --query value -o tsv)
py db.py
# Förväntat: dim_company sync OK
Remove-Item env:DATABASE_URL_ADMIN  # rensa direkt efteråt
```

### Alternativ: bara ETL-credential exponerad

Om du föredrar absolut minst-privilegium default i shell:

```powershell
# Bara skrivande ETL-credential
$env:DATABASE_URL_ETL = (az keyvault secret show --vault-name kv-finauto-6427 `
                          --name database-url-etl --query value -o tsv)
# Webapp + MCP kräver då explicit DATABASE_URL när du startar dem.
# Mer säker men friktion: glömmer du sätta DATABASE_URL går varken
# `uvicorn webapp.backend.main:app` eller MCP igång.
```

## Vad som faktiskt kördes 2026-05-25 (live-spår)

1. **Lösenord:** CSPRNG via `RandomNumberGenerator`, 36-40 tecken base64-trimmat.
2. **Migration:** `_apply.py 20260525_etl_writer_role.sql --var etl_pw=$etlPw`.
   Två förväntade postgres-notices ("no privileges were granted for
   pg_stat_statements{_info}") — extension-vyer pgadmin inte äger.
3. **Verify:** `_verify_t2.py` → 17/17 PASS efter fix att filtrera på
   `pg_tables` (BASE TABLES) istället för `information_schema.tables`
   (som inkluderar extension-vyer).
4. **Smoke som etl_writer:** SELECT 574k rows OK, INSERT (med rollback) OK,
   CREATE TABLE → `psycopg.errors.InsufficientPrivilege` ✅.
5. **db.py-skyddstester:**
   - `connect()` med admin-URL → RuntimeError "ETL ansluten som 'pgadmin'…"
   - `connect()` med etl_writer-URL → returnerar etl_writer
   - `connect(role='admin')` med admin-URL → returnerar pgadmin (ingen check)
6. **KV-secret:** `az keyvault secret set --name database-url-etl
   --tags purpose=etl_writer task=T2`. Read-back-verifierad.

## Acceptanskriterier (alla PASS)

- `etl_writer` finns, `rolsuper=false`, `rolbypassrls=false`, `rolcreatedb=false`,
  `rolcreaterole=false`, ej medlem i `azure_pg_admin`/`pg_write_all_data`
- `statement_timeout=600s` (10 min — täcker tunga load_sie-körningar)
- INGEN `default_transaction_read_only` (vi vill skriva)
- DML (SELECT/INSERT/UPDATE/DELETE/TRUNCATE) på alla 12 pgadmin-ägda
  public-tabeller
- `USAGE + SELECT` på alla 8 public-sequences (för `DEFAULT nextval(...)`
  i INSERTs)
- INGEN CREATE på schema public, INGEN CREATE på databasen `finance`
- `db.connect()` default → role='etl' → fail-fast om admin-credentials

## Rollback

```powershell
# 1. Återgå till admin-credential för ETL (TEMPORÄRT — säkerhetsregression):
#    Använd connect(role='admin') i loaders, eller sätt DATABASE_URL_ETL = admin-URL.
#    Endast om kritisk loader-bug i den nya rolluppsättningen.

# 2. Ta bort rollen helt (om vi vill rulla tillbaka helt):
$env:DATABASE_URL_ADMIN = az keyvault secret show --vault-name kv-finauto-6427 `
  --name database-url --query value -o tsv
.venv\Scripts\python.exe -c @"
import os, psycopg
with psycopg.connect(os.environ['DATABASE_URL_ADMIN'], autocommit=True) as c:
    with c.cursor() as cur:
        cur.execute('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM etl_writer')
        cur.execute('REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM etl_writer')
        cur.execute('REVOKE ALL ON SCHEMA public FROM etl_writer')
        cur.execute('REVOKE ALL ON DATABASE finance FROM etl_writer')
        cur.execute('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM etl_writer')
        cur.execute('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM etl_writer')
        cur.execute('DROP ROLE etl_writer')
"@

# 3. Ta bort KV-secret:
az keyvault secret delete --vault-name kv-finauto-6427 --name database-url-etl
```

## Beroenden / nästa steg

- **T7 (rotera pgadmin)**: nu möjligt — varken MCP eller ETL beror på admin-credential.
  Men vänta gärna tills T3 också är klart så vi inte måste rotera två gånger.
- **T9 (webapp-säkerhet)**: webapp använder fortfarande `db.database_url()` →
  legacy `DATABASE_URL` → admin. T9 byter webapp till egen roll (eller delar
  mcp_readonly för läsning).
- **db.py-skyddet är opt-out**: om någon explicit anropar `connect(role='admin')`
  görs ingen check. Avsiktligt — schema-init behöver vägen.
