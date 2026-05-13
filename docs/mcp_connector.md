# MCP-server — Claude.ai Custom Connector

För kollegor som vill ställa frågor mot finance-warehouse direkt i Claude.ai.

## Vad kollegan får

- **URL:** `https://app-finauto-mcp-6427.azurewebsites.net/mcp`
- **Token:** delad bearer-token (admin skickar ut den separat — *kopiera den, den hashas i KV och kan inte återställas*).

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

`%APPDATA%\Claude\claude_desktop_config.json` (Windows) eller
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "finance-warehouse": {
      "url": "https://app-finauto-mcp-6427.azurewebsites.net/mcp",
      "headers": { "Authorization": "Bearer <din-token>" }
    }
  }
}
```

Starta om Claude Desktop. Tools-ikonen (klippet) ska visa de två verktygen.

## Använda

Ställ frågor på svenska — Claude kallar verktygen automatiskt:

> *Hur många bolag har laddat data för 202604?*

> *Lista de 10 leverantörer med högst spend 2024 i Sverige.*

> *Vilka SE-bolag saknas i facit för april 2026?*

`describe_schema` läses in i kontexten innan första queryn, så Claude kan
period-typer (YTD vs monthly), source_kind-prioriteten, och tecken­konventionen.

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
