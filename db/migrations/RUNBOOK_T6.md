# RUNBOOK T6 — Nätverk/TLS-audit

**Status:** ✅ live i prod (2026-05-25)
**SPEC:** `finance-warehouse_security_remediation_SPEC.md` §T6
**Owner:** [DevOps] — Claude Code genererade kommandona, exekverade direkt
**Server:** `psql-finauto-6427.postgres.database.azure.com` (rg-finauto-6427)

---

## Mål (per SPEC)

- Ingen regel `0.0.0.0–255.255.255.255`; ingen "Allow public access from any Azure service" om det inte krävs.
- `require_secure_transport = ON` (TLS tvingat). Alla connection strings ska ha `sslmode=require`.
- Överväg Private Endpoint/VNet-integration.

## Genomförda ändringar

### Firewall — rensa duplikat
- **Tog bort:** `FirewallIPAddress_2026-5-8_9-52-8` (84.55.89.253 → 84.55.89.253)
  — duplikat av `MyIp-84-55-89-253`. Auto-genererade namn från Azure-portalens
  "Add my IP"-knapp. Konsekvens: ingen — `MyIp-84-55-89-253` täcker samma IP.

### TLS — explicit-sätta parametrar
- **`require_secure_transport`**: `on` (system-default) → `on` (user-override)
  — säkrar att en framtida Azure-default-ändring inte tar bort TLS-kravet.
- **`ssl_min_protocol_version`**: `TLSV1.2` (system-default) → `TLSV1.2` (user-override)
  — samma defensiva mönster. TLS 1.0/1.1 kan inte tystas in av en framtida default.

## Post-state (verifierat 2026-05-25)

| Firewall-regel | Start | End | Behåll? | Anledning |
|---|---|---|---|---|
| `MyIp-84-55-89-253` | 84.55.89.253 | 84.55.89.253 | ✅ | Didriks hemma-IP, dev/admin |
| `MyIp-213-136-53-92` | 213.136.53.92 | 213.136.53.92 | ✅ | Sekundär IP (Prosero?) |
| `AllowAllAzureServicesAndResourcesWithinAzureIps` | 0.0.0.0 | 0.0.0.0 | ✅ load-bearing | App Services saknar VNet — outbound går via Azure publik-IP-pool |

| Parameter | Värde | Source |
|---|---|---|
| `require_secure_transport` | `on` | **user-override** ✅ |
| `ssl_min_protocol_version` | `TLSV1.2` | **user-override** ✅ |

## Verifierings-kommandon

```bash
RG=rg-finauto-6427; SRV=psql-finauto-6427

# Lista firewall (förväntat: 3 regler)
az postgres flexible-server firewall-rule list -g $RG -n $SRV -o table

# TLS-status (förväntat: user-override on)
az postgres flexible-server parameter show -g $RG -s $SRV \
  --name require_secure_transport --query "{value:value, source:source}"

# App Service connection model (förväntat: vnet=null)
az webapp list -g $RG --query "[].{name:name, vnet:virtualNetworkSubnetId}" -o table
```

## Bevarad rest-risk

**`AllowAllAzureServicesAndResourcesWithinAzureIps`** tillåter alla Azure-tenants att
försöka ansluta (men de behöver fortfarande lösenord). Mitigeras av:

- TLS (`sslmode=require`) — försvårar avlyssning
- Dedikerade roller med minsta rättigheter (T1-T3)
- pgaudit-loggning (T4)
- Statement timeout 60s på mcp_readonly (T9-fu)

**Restrisk:** brute-force-försök från andra Azure-tenants. Mitigeringen är att
flytta till Private Endpoint + VNet, vilket är **separat fas** (1-2 dagar arbete,
påverkar App Service-deployment).

## Pre-state (för referens)

Firewall hade 4 regler innan T6:

| Regel | Status |
|---|---|
| `MyIp-84-55-89-253` | ✅ behållen |
| `MyIp-213-136-53-92` | ✅ behållen |
| `AllowAllAzureServicesAndResourcesWithinAzureIps` | ✅ behållen (load-bearing) |
| `FirewallIPAddress_2026-5-8_9-52-8` | ❌ tagen bort (duplikat) |

`require_secure_transport = on` var `system-default` — TLS var tvingat men berodde
på att Azure inte bytt default. Nu **user-override** — egen kontroll.

## Beroenden / nästa steg

- **Private Endpoint-migration** (separat fas): kräver VNet-integration av båda
  App Services (`app-finauto-6427` + `app-finauto-mcp-6427`). Sen kan
  `AllowAllAzureServicesAndResourcesWithinAzureIps` tas bort + publicNetworkAccess
  sättas till Disabled.
- Inga andra T-uppgifter beror på T6.

## Commit

```
chore(db): T6 — network/TLS audit (firewall cleanup + TLS user-override)
```
