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
import json
import logging
import os
import pathlib
import re
import threading
import time

import pandas as pd
import psycopg

# Azure SDK loggar pratigt till stderr vilket korruperar MCP stdio-protokollet.
# Stäng allt under WARNING för azure.*-trädet.
for _name in ("azure", "azure.identity", "azure.core", "azure.keyvault", "httpx", "httpcore", "urllib3"):
    logging.getLogger(_name).setLevel(logging.WARNING)
os.environ.setdefault("AZURE_LOG_LEVEL", "warning")
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings

ROOT = pathlib.Path(__file__).parent
SCHEMA_MD = ROOT / "SCHEMA.md"
LOG_PATH = ROOT / "_logs" / "mcp_queries.jsonl"
TOKEN_PATH = ROOT / ".mcp_token"

KEYVAULT_NAME = os.environ.get("AZURE_KEYVAULT_NAME", "kv-finauto-6427")
KEYVAULT_SECRET_NAME = os.environ.get("AZURE_KEYVAULT_SECRET", "database-url")

QUERY_TIMEOUT_SEC = 30.0
DEFAULT_LIMIT = 1000
PREVIEW_ROWS = 50

HTTP_HOST = "127.0.0.1"
HTTP_PORT = 8765

WRITE_PATTERN = re.compile(
    r"\b(insert|update|delete|create|drop|alter|attach|detach|"
    r"copy|truncate|vacuum|grant|revoke)\b",
    re.IGNORECASE,
)
LIMIT_PATTERN = re.compile(r"\blimit\b\s+\d+", re.IGNORECASE)

mcp = FastMCP(
    "finance-warehouse",
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


def _connect() -> psycopg.Connection:
    return psycopg.connect(_resolve_database_url(), autocommit=True)


def _log(entry: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


@mcp.tool()
def describe_schema() -> str:
    """Returnera warehouse-schema (SCHEMA.md) plus live tabellöversikt
    med radantal per tabell. Anropa detta först när du ska skriva en query."""
    parts: list[str] = []
    if SCHEMA_MD.exists():
        parts.append(SCHEMA_MD.read_text(encoding="utf-8"))
    else:
        parts.append("(SCHEMA.md saknas)")

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            ORDER BY table_name
            """
        )
        tables = [r[0] for r in cur.fetchall()]
        rows = []
        for name in tables:
            cur.execute(f'SELECT COUNT(*) FROM "{name}"')
            rows.append((name, cur.fetchone()[0]))

    parts.append("\n## Live snapshot\n")
    parts.append("| Tabell | Rader |")
    parts.append("|---|---:|")
    for name, count in rows:
        parts.append(f"| `{name}` | {count:,} |")
    return "\n".join(parts)


@mcp.tool()
def query_sql(sql: str, limit: int = DEFAULT_LIMIT) -> str:
    """Kör en SELECT-query mot warehouse. Read-only.

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
    conn = _connect()
    # statement_timeout på session-nivå räcker som primär timeout-mekanism.
    # Threading.Timer + conn.cancel() är belt-and-braces (cancel kräver
    # secondary connection och kan ta tid att verka).
    with conn.cursor() as cur:
        # SET tar inte parametrar i Postgres, embedda literal.
        cur.execute(f"SET statement_timeout = {int(QUERY_TIMEOUT_SEC * 1000)}")

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
    except Exception as exc:
        timer.cancel()
        conn.close()
        _log({"sql": sql, "ok": False, "error": str(exc)})
        return f"ERROR: {exc}"
    finally:
        timer.cancel()
        conn.close()

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
    if not TOKEN_PATH.exists():
        raise SystemExit(
            f"Token saknas: {TOKEN_PATH}\n"
            "Generera en med:\n"
            '  py -c "import secrets; print(secrets.token_urlsafe(32))" > .mcp_token'
        )
    return TOKEN_PATH.read_text(encoding="utf-8").strip()


def _run_http() -> None:
    """Starta streamable-http med bearer-token-auth.

    Accepterar token i två former:
      - Authorization: Bearer <token>   (primärt)
      - ?token=<token>                  (fallback för UI:er som inte har headers-fält)
    """
    import uvicorn
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    expected = _load_token()

    class BearerAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            header = request.headers.get("authorization", "")
            token = header[7:] if header.lower().startswith("bearer ") else ""
            if not token:
                token = request.query_params.get("token", "")
            if token != expected:
                return JSONResponse(
                    {"error": "unauthorized"}, status_code=401
                )
            return await call_next(request)

    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuthMiddleware)
    print(f"Startar streamable-http på http://{HTTP_HOST}:{HTTP_PORT}/mcp")
    print(f"Token (kopiera till Connector): {expected}")
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
