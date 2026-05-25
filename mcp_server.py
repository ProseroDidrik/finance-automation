"""MCP-server som exponerar warehouse (Postgres) read-only.

Tools:
  - describe_schema  : SCHEMA.md + live tabellöversikt med radantal
  - query_sql        : fri SELECT med radtak, timeout och DML-blockering

Två transporter:
  - stdio  (default): startas av Claude Code via .mcp.json
  - http   (--http) : streamable-http på 127.0.0.1:8765/mcp för Claude Desktop

Connection: läser ``DATABASE_URL`` från miljön. Om den saknas hämtas
secret ``database-url`` från Azure Key Vault (``kv-finauto-6427``) via
``DefaultAzureCredential`` — så fungerar servern transparent när
Claude Code startar den utan att shell-env ärvs, så länge användaren
är ``az login``-ad.
"""
from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
import pathlib
import re
import threading
import time

import pandas as pd
import psycopg
from psycopg_pool import ConnectionPool

# Azure SDK loggar pratigt till stderr vilket korruperar MCP stdio-protokollet.
# Stäng allt under WARNING för azure.*-trädet.
for _name in ("azure", "azure.identity", "azure.core", "azure.keyvault", "httpx", "httpcore", "urllib3"):
    logging.getLogger(_name).setLevel(logging.WARNING)
os.environ.setdefault("AZURE_LOG_LEVEL", "warning")
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings

ROOT = pathlib.Path(__file__).parent
SCHEMA_MD = ROOT / "SCHEMA.md"
SEMANTICS_MD = ROOT / "docs" / "warehouse_semantics.md"
LOG_PATH = ROOT / "_logs" / "mcp_queries.jsonl"
TOKEN_PATH = ROOT / ".mcp_token"

KEYVAULT_NAME = os.environ.get("AZURE_KEYVAULT_NAME", "kv-finauto-6427")
# T1 (2026-05-25): default ändrad från "database-url" (admin) till
# "database-url-readonly" (mcp_readonly-rollen). MCP:n ska aldrig ansluta som
# pgadmin — adminkontot är break-glass. Sätt AZURE_KEYVAULT_SECRET=database-url
# om du explicit behöver admin (t.ex. för debug — ingen runtime-flöde gör det).
KEYVAULT_SECRET_NAME = os.environ.get("AZURE_KEYVAULT_SECRET", "database-url-readonly")

QUERY_TIMEOUT_SEC = 30.0
DEFAULT_LIMIT = 1000
PREVIEW_ROWS = 50

HTTP_HOST = os.environ.get("MCP_HTTP_HOST", "127.0.0.1")
HTTP_PORT = int(os.environ.get("PORT", os.environ.get("MCP_HTTP_PORT", "8765")))

WRITE_PATTERN = re.compile(
    r"\b(insert|update|delete|create|drop|alter|attach|detach|"
    r"copy|truncate|vacuum|grant|revoke)\b",
    re.IGNORECASE,
)
LIMIT_PATTERN = re.compile(r"\blimit\b\s+\d+", re.IGNORECASE)

# Skickas i MCP:s ``initialize``-svar — klienten (Claude.ai/Desktop) ser det
# INNAN något verktyg anropas. Det är enda stället att säga "kör describe_schema
# först" där modellen läser det utan att redan ha valt att anropa describe_schema.
# Håll kort — det ligger i varje handshake. Detaljerna bor i describe_schema.
SERVER_INSTRUCTIONS = """\
finance-warehouse — Prosero-koncernens nordiska ekonomidata (Postgres, read-only).

ARBETSORDNING — varje ny konversation:
1. Anropa `describe_schema` EN gång före din första `query_sql`. Det returnerar
   tabeller, live radantal OCH query-semantiken. Hoppar du över det räknar du fel.
2. Skriv sedan `query_sql` (read-only SELECT).

Fyra fällor som ger tyst FELAKTIGA siffror om du inte läst describe_schema:
- `fact_balances.amount` är YTD (ackumulerat sedan 1 jan) för SE/NO men
  månadsrörelse för FI/DK/DE. SUM:a aldrig `amount` rakt över länder.
- Samma (bolag, period) kan ha flera `source_kind`. Välj högsta prioritet per
  land (best_source) — summera aldrig över källor.
- Filtrera alltid `scenario = 'A'` för utfall, annars dubblas budget in.
- Teckenkonvention är SIE (intäkt negativ); `P_*`-konton är teckenflippade.

describe_schema förklarar alla fyra med färdiga SQL-mönster. Dialekt: Postgres
(`to_char`, inte `strftime`). Vid osäkerhet: visa SQL:en för användaren innan
stora aggregeringar körs, och svara med en kort sammanfattning — inte råa
radhögar."""

mcp = FastMCP(
    "finance-warehouse",
    instructions=SERVER_INSTRUCTIONS,
    host=HTTP_HOST,
    port=HTTP_PORT,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)


def _resolve_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
    except ImportError as exc:
        raise RuntimeError(
            "DATABASE_URL saknas i miljön och azure-keyvault-secrets är inte installerat. "
            "Antingen: sätt $env:DATABASE_URL eller `pip install azure-identity azure-keyvault-secrets`."
        ) from exc
    vault_url = f"https://{KEYVAULT_NAME}.vault.azure.net"
    client = SecretClient(vault_url=vault_url, credential=DefaultAzureCredential())
    secret = client.get_secret(KEYVAULT_SECRET_NAME)
    os.environ["DATABASE_URL"] = secret.value
    return secret.value


# Connection pool — återanvänder anslutningar i stället för att göra en ny
# TCP+TLS+auth-handshake per tool-anrop. configure() ger varje anslutning
# 30 s statement_timeout.
_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def _configure_conn(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(f"SET statement_timeout = {int(QUERY_TIMEOUT_SEC * 1000)}")


def _get_pool() -> ConnectionPool:
    """Lazy singleton-pool. Skapas vid första anropet (efter att DATABASE_URL
    resolvats, ev. via Key Vault). Fungerar i både stdio- och http-läget."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ConnectionPool(
                    conninfo=_resolve_database_url(),
                    min_size=1,
                    max_size=4,
                    kwargs={"autocommit": True},
                    configure=_configure_conn,
                    open=True,
                )
    return _pool


@atexit.register
def _close_pool() -> None:
    """Stäng poolen rent vid process-shutdown — annars klagar Python 3.13+
    på att pool-trådarna inte kan joinas under interpreter-finalisering."""
    if _pool is not None:
        _pool.close()


def _log(entry: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# describe_schema cachas — anropas i början av varje konversation och behöver
# inte vara färskare än så. reltuples är ungefärligt (uppdateras av ANALYZE).
SCHEMA_CACHE_TTL = 300.0  # sekunder
_schema_cache: tuple[float, str] | None = None
_schema_cache_lock = threading.Lock()


def _build_schema_snapshot() -> str:
    """Bygg describe_schema-svaret: SCHEMA.md + approx tabellöversikt + semantik.

    Tabellöversikten använder pg_class.reltuples (ungefärligt, ~momentant) i
    stället för exakt COUNT(*) per tabell, som annars seq-scannar varje tabell.
    """
    parts: list[str] = []
    if SCHEMA_MD.exists():
        parts.append(SCHEMA_MD.read_text(encoding="utf-8"))
    else:
        parts.append("(SCHEMA.md saknas)")

    with _get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname, c.reltuples::bigint
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'r' AND n.nspname = 'public'
            ORDER BY c.relname
            """
        )
        rows = cur.fetchall()

    parts.append("\n## Live snapshot (≈ rader, uppskattning)\n")
    parts.append("| Tabell | ≈ Rader |")
    parts.append("|---|---:|")
    for name, count in rows:
        parts.append(f"| `{name}` | {max(count, 0):,} |")

    # Semantik-reglerna (period_type, best_source, etc.) — finns både i
    # repo:t (docs/warehouse_semantics.md) och som lokal skill i Claude Code.
    # Här bakas den in i tool-svaret så Claude.ai/Desktop-användare också får
    # reglerna automatiskt vid första describe_schema-anropet.
    if SEMANTICS_MD.exists():
        parts.append("\n\n---\n\n# Query-semantik (läs detta innan du skriver SQL)\n")
        parts.append(SEMANTICS_MD.read_text(encoding="utf-8"))

    return "\n".join(parts)


@mcp.tool()
def describe_schema() -> str:
    """Returnera warehouse-schema (SCHEMA.md), live tabellöversikt med approx
    radantal, plus query-semantik (warehouse_semantics.md) som täcker
    period_type, best_source-prioritet per land, scenario-filter,
    teckenkonvention och facit-jämförelse. Anropa detta FÖRST när du ska skriva
    en query — utan semantik-reglerna räknar du fel.

    Svaret cachas i 5 min — radantalen är ungefärliga (pg_class.reltuples).
    """
    global _schema_cache
    now = time.time()
    cached = _schema_cache
    if cached is not None and now - cached[0] < SCHEMA_CACHE_TTL:
        return cached[1]
    snapshot = _build_schema_snapshot()
    with _schema_cache_lock:
        _schema_cache = (time.time(), snapshot)
    return snapshot


@mcp.tool()
def query_sql(sql: str, limit: int = DEFAULT_LIMIT) -> str:
    """Kör en SELECT-query mot warehouse. Read-only.

    Kör `describe_schema` först om du inte redan gjort det i den här
    konversationen — semantiken där (YTD vs monthly, best_source, scenario,
    teckenkonvention) avgör om dina siffror blir rätt.

    - DML/DDL avvisas (INSERT/UPDATE/DELETE/CREATE/DROP/ATTACH/COPY/TRUNCATE/...).
    - Om ingen LIMIT finns i queryn slås den på automatiskt (max `limit` rader).
    - Timeout: 30 sekunder.
    - Resultat ≤50 rader → markdown-tabell. Större → preview + summary.

    Args:
        sql: SQL-sträng. Endast SELECT/WITH/SHOW/DESCRIBE.
        limit: Övre tak på rader om ingen LIMIT finns i queryn.
    """
    if WRITE_PATTERN.search(sql):
        return "ERROR: Endast read-only-queries tillåtna (SELECT/WITH/DESCRIBE/SHOW)."

    sql_to_run = sql.strip().rstrip(";")
    if not LIMIT_PATTERN.search(sql_to_run):
        sql_to_run = f"SELECT * FROM ({sql_to_run}) AS _q LIMIT {int(limit)}"

    t0 = time.time()
    # statement_timeout sätts per anslutning av poolens _configure_conn.
    # Threading.Timer + conn.cancel() är belt-and-braces (cancel kräver
    # secondary connection och kan ta tid att verka).
    try:
        with _get_pool().connection() as conn:
            def _cancel() -> None:
                try:
                    conn.cancel()
                except Exception:
                    pass

            timer = threading.Timer(QUERY_TIMEOUT_SEC + 2, _cancel)
            timer.start()
            try:
                with conn.cursor() as cur:
                    cur.execute(sql_to_run)
                    columns = [d[0] for d in cur.description] if cur.description else []
                    rows = cur.fetchall()
                df = pd.DataFrame(rows, columns=columns)
            finally:
                timer.cancel()
    except Exception as exc:
        _log({"sql": sql, "ok": False, "error": str(exc)})
        return f"ERROR: {exc}"

    elapsed_ms = int((time.time() - t0) * 1000)
    n = len(df)
    _log({"sql": sql, "rows": n, "ms": elapsed_ms, "ok": True})

    if n == 0:
        return f"(0 rader, {elapsed_ms} ms)"

    if n <= PREVIEW_ROWS:
        return f"{n} rader, {elapsed_ms} ms\n\n{df.to_markdown(index=False)}"

    head = df.head(PREVIEW_ROWS).to_markdown(index=False)
    return (
        f"{n} rader, {elapsed_ms} ms — visar de första {PREVIEW_ROWS}:\n\n{head}\n\n"
        f"_(höj `limit` eller smalna queryn för fler rader)_"
    )


def _load_token() -> str:
    """Token-prio: MCP_BEARER_TOKEN env → .mcp_token-fil (lokal dev).

    I Azure App Service sätts ``MCP_BEARER_TOKEN`` som @Microsoft.KeyVault-ref
    mot ``kv-finauto-6427/mcp-bearer-token``. Lokalt kör vi mot .mcp_token-filen
    så stdio-läget och http-läget delar mönster utan att kräva env.
    """
    env_token = os.environ.get("MCP_BEARER_TOKEN", "").strip()
    if env_token:
        return env_token
    if not TOKEN_PATH.exists():
        raise SystemExit(
            f"Token saknas: varken MCP_BEARER_TOKEN-env eller {TOKEN_PATH} hittades.\n"
            "Generera en lokal med:\n"
            '  py -c "import secrets; print(secrets.token_urlsafe(32))" > .mcp_token'
        )
    return TOKEN_PATH.read_text(encoding="utf-8").strip()


def _run_http() -> None:
    """Starta streamable-http med bearer-token-auth.

    Accepterar token i två former:
      - Authorization: Bearer <token>   (primärt)
      - ?token=<token>                  (fallback för UI:er som inte har headers-fält)

    /healthz är ALLTID public — Azure App Service liveness-probe pingar den utan
    Authorization-header och skulle annars få 401 → unhealthy.
    """
    import uvicorn
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse, PlainTextResponse
    from starlette.routing import Route

    expected = _load_token()

    class BearerAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.url.path == "/healthz":
                return await call_next(request)
            header = request.headers.get("authorization", "")
            token = header[7:] if header.lower().startswith("bearer ") else ""
            if not token:
                token = request.query_params.get("token", "")
            if token != expected:
                return JSONResponse(
                    {"error": "unauthorized"}, status_code=401
                )
            return await call_next(request)

    async def _healthz(_request):
        return PlainTextResponse("ok")

    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuthMiddleware)
    app.router.routes.append(Route("/healthz", _healthz, methods=["GET"]))
    print(f"Startar streamable-http på http://{HTTP_HOST}:{HTTP_PORT}/mcp")
    # Logga bara hash av token, inte token:en själv — containerloggar i Azure är
    # ofta synliga för fler ögon än vi vill.
    import hashlib
    token_hash = hashlib.sha256(expected.encode()).hexdigest()[:12]
    print(f"Token-hash (sha256[:12]): {token_hash}")
    uvicorn.run(app, host=HTTP_HOST, port=HTTP_PORT, log_level="info")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--http",
        action="store_true",
        help=f"Kör som streamable-http på {HTTP_HOST}:{HTTP_PORT}/mcp "
        "(för Claude Desktop Custom Connector). Default = stdio.",
    )
    args = parser.parse_args()
    if args.http:
        _run_http()
    else:
        mcp.run()
