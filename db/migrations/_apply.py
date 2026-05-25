"""Apply a psql-style migration via psycopg — så vi inte behöver psql installerat.

Pre-processar psql-meta-kommandon som migrationerna använder för portabilitet:
  \\set ON_ERROR_STOP on/off  → ignoreras (psycopg höjer alltid vid fel)
  \\echo 'text'               → skrivs till stdout AFTER lyckad körning
  \\pset ...                  → ignoreras
  :'varname'                  → ersätts med SQL-quoted literal av --var-värdet

Anropas typiskt så här (PowerShell):
    $env:DATABASE_URL_ADMIN = az keyvault secret show --vault-name kv-finauto-6427 `
        --name database-url --query value -o tsv
    .venv\\Scripts\\python.exe db\\migrations\\_apply.py `
        db\\migrations\\20260525_mcp_readonly_role.sql --var mcp_pw=$mcpPw

Säkerhet:
- DATABASE_URL_ADMIN läses från env, aldrig från fil/argv.
- --var-värdena exponeras i process-listan kort — undvik att skriva dem på CLI
  i delade miljöer. För hemligheter, exportera via env och peka --var till env.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import psycopg

PSQL_VAR_RE = re.compile(r":'([A-Za-z_][A-Za-z0-9_]*)'")


def _sql_literal(value: str) -> str:
    """Säker SQL-string-literal — escapar enkla citationstecken."""
    return "'" + value.replace("'", "''") + "'"


def _preprocess(sql: str, variables: dict[str, str]) -> tuple[str, list[str]]:
    """Strippa psql-meta-kommandon. Returnerar (clean_sql, echo_lines)."""
    cleaned: list[str] = []
    echoes: list[str] = []

    for line in sql.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("\\echo"):
            text = stripped[len("\\echo"):].strip()
            if (text.startswith("'") and text.endswith("'")) or (
                text.startswith('"') and text.endswith('"')
            ):
                text = text[1:-1]
            echoes.append(text)
            cleaned.append("")  # behåll rad-numrering för felmeddelanden
            continue
        if stripped.startswith("\\"):
            cleaned.append("")
            continue
        cleaned.append(line)

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables:
            raise SystemExit(
                f"Migration använder :'{name}' men ingen --var {name}=... gavs"
            )
        return _sql_literal(variables[name])

    # Substituera per rad, och bara på del FÖRE ev. `--`-kommentar på samma rad.
    # Skyddar mot att hemligheter läcker in i SQL-kommentarer (som annars skickas
    # till servern och kan dyka upp i log_statement-utskrifter).
    # Förenklat: vi tar första `--` som inte är inuti en enkel-citerad sträng;
    # täcker våra migrationer (dollar-quoted strängar innehåller normalt inte `--`).
    def _substitute_line(line: str) -> str:
        comment_at = _find_inline_comment(line)
        if comment_at is None:
            return PSQL_VAR_RE.sub(_sub, line)
        return PSQL_VAR_RE.sub(_sub, line[:comment_at]) + line[comment_at:]

    clean_sql = "\n".join(_substitute_line(line) for line in cleaned)
    return clean_sql, echoes


def _find_inline_comment(line: str) -> int | None:
    """Returnerar index för första `--` som inte är inuti en '...'-literal,
    eller None om ingen finns. Förenklad: hanterar inte $$...$$ men det räcker
    för våra migrationers radvisa kommentarer."""
    i = 0
    in_quote = False
    while i < len(line) - 1:
        c = line[i]
        if c == "'":
            in_quote = not in_quote
        elif not in_quote and c == "-" and line[i + 1] == "-":
            return i
        i += 1
    return None


def main() -> int:
    # Windows-konsol är cp1252 default; våra migrationer + \echo-rader
    # innehåller UTF-8-tecken (→, ★, å/ö). Reconfigure stdout/stderr så
    # printen inte kraschar på unikod. Säker även om migrationen har
    # commitats — bara utskriften skulle ha failat.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass  # äldre Python eller pipe utan reconfigure

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sql_file", help="Sökväg till migration-.sql")
    parser.add_argument(
        "--var",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="psql-variabelvärde — flera --var kan ges",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Pre-processa och skriv ut SQL:en utan att köra (lösenord maskeras)",
    )
    args = parser.parse_args()

    variables: dict[str, str] = {}
    for v in args.var:
        if "=" not in v:
            parser.error(f"--var måste vara NAME=VALUE, fick {v!r}")
        name, _, value = v.partition("=")
        variables[name] = value

    sql_path = Path(args.sql_file)
    if not sql_path.exists():
        parser.error(f"hittar inte: {sql_path}")

    raw = sql_path.read_text(encoding="utf-8")
    clean_sql, echoes = _preprocess(raw, variables)

    if args.dry_run:
        masked = clean_sql
        for value in variables.values():
            if value:
                masked = masked.replace(_sql_literal(value), "'***MASKED***'")
        print(masked)
        for line in echoes:
            print(f"  (echo) {line}", file=sys.stderr)
        return 0

    db_url = os.environ.get("DATABASE_URL_ADMIN")
    if not db_url:
        sys.exit("DATABASE_URL_ADMIN saknas i env — exportera admin-strängen från KV")

    print(f"[apply] {sql_path}", file=sys.stderr)
    notices: list[str] = []
    with psycopg.connect(db_url, autocommit=False) as conn:
        # psycopg 3: notices via handler, inte conn.notices (det var psycopg 2).
        # Vi samlar NOTICE/WARNING från PL/pgSQL (t.ex. RAISE NOTICE i DO-block).
        conn.add_notice_handler(lambda diag: notices.append(diag.message_primary))
        with conn.cursor() as cur:
            cur.execute(clean_sql)
        conn.commit()
    for n in notices:
        print(f"  NOTICE: {n}", file=sys.stderr)

    for line in echoes:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
