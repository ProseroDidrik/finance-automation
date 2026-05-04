"""MCP-server som exponerar warehouse (data/finance.duckdb) read-only.

Tools (Fas 1):
  - describe_schema  : SCHEMA.md + live tabellöversikt
  - query_sql        : fri SELECT med radtak, timeout och DML-blockering

Två transporter:
  - stdio  (default): startas av Claude Code via .mcp.json
  - http   (--http) : streamable-http på 127.0.0.1:8765/mcp för Claude Desktop
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import threading
import time

import duckdb
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings

ROOT = pathlib.Path(__file__).parent
DB_PATH = ROOT / "data" / "finance.duckdb"
SCHEMA_MD = ROOT / "SCHEMA.md"
LOG_PATH = ROOT / "_logs" / "mcp_queries.jsonl"
TOKEN_PATH = ROOT / ".mcp_token"

QUERY_TIMEOUT_SEC = 30.0
DEFAULT_LIMIT = 1000
PREVIEW_ROWS = 50

HTTP_HOST = "127.0.0.1"
HTTP_PORT = 8765

WRITE_PATTERN = re.compile(
    r"\b(insert|update|delete|create|drop|alter|attach|detach|"
    r"copy|pragma|truncate|vacuum|set|export|import|use)\b",
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


def _connect() -> duckdb.DuckDBPyConnection:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Warehouse saknas: {DB_PATH}")
    return duckdb.connect(str(DB_PATH), read_only=True)


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

    with _connect() as con:
        tables = con.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'main'
            ORDER BY table_name
            """
        ).fetchall()

        rows = []
        for (name,) in tables:
            count = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            rows.append((name, count))

    parts.append("\n## Live snapshot\n")
    parts.append("| Tabell | Rader |")
    parts.append("|---|---:|")
    for name, count in rows:
        parts.append(f"| `{name}` | {count:,} |")
    return "\n".join(parts)


@mcp.tool()
def query_sql(sql: str, limit: int = DEFAULT_LIMIT) -> str:
    """Kör en SELECT-query mot warehouse. Read-only.

    - DML/DDL avvisas (INSERT/UPDATE/DELETE/CREATE/DROP/ATTACH/COPY/PRAGMA/...).
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
    con = _connect()
    timer = threading.Timer(QUERY_TIMEOUT_SEC, con.interrupt)
    timer.start()
    try:
        df = con.execute(sql_to_run).fetchdf()
    except Exception as exc:
        timer.cancel()
        con.close()
        _log({"sql": sql, "ok": False, "error": str(exc)})
        return f"ERROR: {exc}"
    finally:
        timer.cancel()
        con.close()

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
