# MCP-server — Claude.ai Custom Connector

För kollegor som vill ställa frågor mot finance-warehouse direkt i Claude.ai.

## Vad kollegan får

- **URL:** `https://app-finauto-mcp-6427.azurewebsites.net/mcp`
- **Token:** delad bearer-token (admin skickar ut den separat). Den lagras som
  klartext-secret `mcp-bearer-token` i Key Vault och kan hämtas av admin med
  `az keyvault secret show --vault-name kv-finauto-6427 --name mcp-bearer-token --query value --output tsv`.
  (Hashen `sha256[:12]` som `bootstrap_mcp.ps1` skriver ut är bara ett
  fingeravtryck för loggning — inte hur token:en lagras.)

Två tools blir tillgängliga i Claude.ai-konversationen så fort connectorn är ansluten:

| Tool | Vad det gör |
|---|---|
| `describe_schema` | Returnerar SCHEMA.md + live tabellöversikt med radantal. Kör alltid detta först innan du skriver en query. |
| `query_sql` | Read-only SELECT mot warehouse:t. DML/DDL avvisas. Auto-LIMIT 1000 om query saknar LIMIT. 30 s timeout. |

## Lägg till i Claude.ai

1. Logga in på [claude.ai](https://claude.ai).
2. Profilmenyn (nere till vänster) → **Settings** → **Connectors**.
3. **Add custom connector**.
4. Fyll i:
   - **Name:** *Finance Warehouse* (eller valfritt)
   - **Remote MCP server URL:** `https://app-finauto-mcp-6427.azurewebsites.net/mcp`
   - **Authentication:** *Bearer token* → klistra in token:en.
5. Spara → verifiera att Claude.ai skriver "Connected" + listar `describe_schema` och `query_sql`.

## Lägg till i Claude Desktop

Claude Desktop's JSON-config stöder **bara stdio-transport** (`command`/`args`),
inte direkt `url`-format. Vi använder `mcp-remote` som stdio-↔-HTTP-brygga.
**Kräver Node.js** på maskinen (`winget install OpenJS.NodeJS` på Windows,
eller https://nodejs.org/).

Öppna config-filen via **Settings → Developer → Edit Config** (eller direkt):
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

Klistra in (eller merge `mcpServers`-blocket med befintliga inställningar):

```json
{
  "mcpServers": {
    "finance-warehouse": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://app-finauto-mcp-6427.azurewebsites.net/mcp",
        "--header",
        "Authorization:Bearer <din-token>"
      ]
    }
  }
}
```

Spara, **avsluta Claude Desktop helt från system tray** (inte bara stäng
fönstret), starta igen. Första starten tar ~30 s extra medan `npx -y` hämtar
`mcp-remote`-paketet. Sedan ska tools-ikonen (klippet/skiftnyckeln) i chatten
visa de två verktygen.

**Connectors-UI:n i Settings stöder inte vår bearer-token** (den kräver OAuth
2.0 discovery). Den UI-vägen fungerar inte än — använd JSON-configen ovan.

## Använda

Ställ frågor på svenska — Claude kallar verktygen automatiskt:

> *Hur många bolag har laddat data för 202604?*

> *Lista de 10 leverantörer med högst spend 2024 i Sverige.*

> *Vilka SE-bolag saknas i facit för april 2026?*

Claude ska anropa `describe_schema` själv innan första queryn — det ger
schema + query-semantik (YTD vs monthly, source_kind-prioritet, tecken­-
konvention). Servern påminner om det via sitt `instructions`-fält, men för
stabilast resultat: kör frågorna i en Claude Project med instruktionerna nedan.

## Project-instruktioner (rekommenderas starkt)

En lös chatt utan kontext gör ofta att Claude hoppar direkt på `query_sql`,
gissar schemat och räknar fel. Lägg verktygsanvändarna i en delad **Claude
Project** och klistra in följande i projektets instruktioner — admin gör det
en gång, det gäller alla i projektet och funkar i både claude.ai och Claude
Desktop:

```text
Du har MCP-servern "finance-warehouse" — Prosero-koncernens nordiska
ekonomidata (Postgres, read-only) med verktygen describe_schema och query_sql.

Arbetsordning för varje fråga om ekonomidata:
1. Anropa describe_schema EN gång i början av konversationen, innan du skriver
   någon SQL. Det ger tabeller, live radantal och query-semantiken.
2. Skriv query_sql (read-only SELECT) enligt den semantiken.

Räkna inte fel — fyra återkommande fällor:
- fact_balances.amount är YTD (ackumulerat) för Sverige/Norge men
  månadsrörelse för Finland/Danmark/Tyskland. Summera aldrig amount rakt
  över länder utan att normalisera period_type.
- Samma bolag+period kan ha flera source_kind. Välj högsta prioritet per
  land (best_source) — summera aldrig över källor.
- Filtrera alltid scenario = 'A' för utfall, annars dubblas budget in.
- Teckenkonvention är SIE (intäkt negativ); P_*-konton är teckenflippade.

describe_schema förklarar alla fyra i detalj med färdiga SQL-mönster — följ
dem. Visa SQL:en för användaren innan stora aggregeringar, och svara med en
kort sammanfattning, inte råa radhögar.
```

## Säkerhet

- Read-only — `INSERT/UPDATE/DELETE/CREATE/DROP/...` blockeras på server-sidan,
  *inte bara* via roller på Postgres-nivå.
- Token:en är delad — om någon i teamet slutar eller token läcker, rotera den:
  ```powershell
  .\scripts\bootstrap_mcp.ps1 -Suffix 6427 -RotateToken
  ```
  Sedan måste alla kollegor uppdatera sin connector-config.
- Loggning: varje query landar i `_logs/mcp_queries.jsonl` i container:ns
  filsystem (rensas vid omstart). För persistent audit krävs blob/storage-mount
  — backloggspoint för senare.
- App Service-MI har bara `Key Vault Secrets User` och `AcrPull`. Postgres-
  rollerna styrs av `DATABASE_URL`-användaren (samma som webapp:en — read-only
  via `mcp_server.py`s WRITE_PATTERN-vakt).

## Felsökning

| Symptom | Trolig orsak |
|---|---|
| Claude.ai: "Failed to connect" | URL fel eller token-mismatch. Verifiera med `curl https://app-finauto-mcp-6427.azurewebsites.net/healthz` → ska ge `ok`. |
| `401 unauthorized` | Token-rotation har skett. Hämta ny från admin. |
| `503 DATABASE_URL saknas` | KV-ref kunde inte resolva — MI saknar Key Vault Secrets User, eller secret raderad. Kör `bootstrap_mcp.ps1` igen. |
| Tomma resultat på frågor som borde fungera | Kolla att periodspann ligger inom det som finns i DB (kör `describe_schema` igen för live-radantal). |
