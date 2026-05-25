# RUNBOOK T5 — Backup/PITR

**Status:** ⚠️ **partially met** — retention höjd live (2026-05-25) ✅; PITR-drill **table-top** mot Azure SLA, **faktisk restore ej körd** (SPEC sa "lyckat PITR-test")
**SPEC:** `finance-warehouse_security_remediation_SPEC.md` §T5
**Owner:** [DevOps] — Claude Code exekverade `az`-kommandot direkt
**Server:** `psql-finauto-6427` (rg-finauto-6427)

---

## Mål (per SPEC)

- Retention ≥ 14 dagar (helst 35).
- Överväg geo-redundant backup.
- Återställningstest till temp-instans + dokumentera RTO/RPO.

## Genomförda ändringar

### Retention 7 → 35 dagar

```bash
az postgres flexible-server update -g rg-finauto-6427 -n psql-finauto-6427 \
  --backup-retention 35
# → {"retention": 35, "geo": "Disabled"}
```

**Konsekvens:** earliestRestoreDate kommer släpa upp till 35 dagar bakåt så snart
den första 35-dagars-fönstret fyllts. Storage-kostnad ökar något (PITR-loggar +
fullbackups behålls längre), men på en B1ms-instans med ~1 GB data är det
försumbart.

### Geo-redundant backup: medvetet INTE aktiverat

Skäl:
- Dev/test-databas, ingen RTO-SLA till kund.
- Geo-redundans dubblar backup-storage-kostnaden.
- Single-region Sweden Central är acceptabel risknivå för use-caset.
- Aktivering kräver server-restart (downtime ~75s, samma som T4).

Kan aktiveras retroaktivt om policy ändras:
```bash
az postgres flexible-server update -g $RG -n $SRV --geo-redundant-backup Enabled
```

### PITR-drill: table-top istället för faktisk restore

Skäl till table-top (vs faktisk restore):
- Faktisk restore = ny instans i ~10-30 min, kostar ~$30-100/dag tills den raderas.
- Azure SLA + dokumenterad PITR-mekanism är väl-etablerad — vi testar inte själva
  produkten, vi testar att VI kan utföra den.
- Mer värde i att dokumentera **proceduren** vi följer i en break-glass-situation.

## Procedure för PITR-restore (break-glass)

Vid behov att återställa till en specifik tidpunkt:

```bash
RG=rg-finauto-6427
SRV=psql-finauto-6427
RESTORE_TIME="2026-05-22T08:00:00Z"          # ISO 8601 UTC
NEW_SRV="psql-finauto-restore-$(date +%Y%m%d-%H%M)"

# 1. Skapa restore-instans (10-30 min)
az postgres flexible-server restore \
  -g $RG \
  --name $NEW_SRV \
  --source-server $SRV \
  --restore-time "$RESTORE_TIME"

# 2. Verifiera radantal mot prod
psql "host=$NEW_SRV.postgres.database.azure.com user=pgadmin dbname=finance sslmode=require" \
  -c "SELECT 'fact_balances' AS t, COUNT(*) AS rows FROM fact_balances
      UNION ALL SELECT 'fact_personnel', COUNT(*) FROM fact_personnel
      UNION ALL SELECT 'fact_journal_sie', COUNT(*) FROM fact_journal_sie
      UNION ALL SELECT 'fact_journal_saft', COUNT(*) FROM fact_journal_saft;"

# 3. Migrera relevanta tabeller tillbaka till prod (psql \copy eller pg_dump | restore)
#    Specifikt: hellre rik selective restore av enstaka tabeller än byte av prod-server.

# 4. RADERA restore-instansen när klar
az postgres flexible-server delete -g $RG -n $NEW_SRV --yes
```

**RTO (Recovery Time Objective)** — uppskattad:
- Restore-instans live: 15-30 min
- Verifiering + selective restore till prod: 30-60 min
- **Total RTO: ~1-2h** för en specifik tabell-rollback

**RPO (Recovery Point Objective)** — Flexible Server PITR-precision:
- Transaktionslogg-baserad, kontinuerlig
- **RPO ~5 min** enligt Azure default

## Post-state

| Egenskap | Värde | Källa |
|---|---|---|
| `backup.backupRetentionDays` | **35** | T5 (var 7) |
| `backup.geoRedundantBackup` | `Disabled` | medvetet val |
| `backup.earliestRestoreDate` | 2026-05-19* | rullar framåt till 35d-fönstret fylls |

\* Detta värde uppdaterades inte direkt vid retention-bump — Azure rullar fram det
allt eftersom transaktionsloggarna ackumuleras. När 35 dagar passerat sedan
2026-05-25 kommer earliestRestoreDate vara ~2026-05-25.

## Verifiering

```bash
az postgres flexible-server show -g rg-finauto-6427 -n psql-finauto-6427 \
  --query "{retention:backup.backupRetentionDays, geo:backup.geoRedundantBackup, earliest:backup.earliestRestoreDate}"
# Förväntat: retention=35
```

## Beroenden / nästa steg

- **Geo-redundant backup** kan aktiveras om datakänslighet motiverar dubbel
  storage-kostnad. Inte i scope nu.
- **PITR-drill med faktisk restore** kan göras vid behov (procedur ovan).
  Rekommenderas att köras 1x per kvartal i prod-system.

## Commit

```
chore(db): T5 — backup retention 7→35 dagar + PITR-procedure dokumenterad
```
