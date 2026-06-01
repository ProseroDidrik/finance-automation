"""DB-åtkomst för dashboarden — via repots db.py, read-only.

Dashboarden är en commit:bar CLI och får INTE bero på finance-warehouse-MCP:n
(det är Claudes ad-hoc-verktyg, inte ett bibliotek). Vi går samma väg som
loaders: repots db.connect(). read_only=True ⇒ autocommit, ingen skrivroll krävs.

Skill-queryna returnerar EN rad med en JSON-text-kolumn (`... json_agg(...)::text
AS payload`). run_payload() kör en sådan query och returnerar den parsade listan.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Gör både repo-roten (för db.py) och den egna paketmappen (för queries/config)
# importerbara oavsett om modulen körs som script eller importeras. Repo-roten
# ligger tre nivåer upp: dashboards/ytd_nyckeltal/db_io.py → repo.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]
for _p in (str(_HERE), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import db  # noqa: E402  (repo-rot db.py)
import queries  # noqa: E402
from config import derive_periods  # noqa: E402


def connect():
    """Read-only-anslutning mot warehouse (DATABASE_URL, vilken roll som helst).

    Dashboarden läser bara fact_balances + dim_* + fact_personnel — ingen journal —
    så den read-only mcp_readonly-rollen räcker. role='legacy' = DATABASE_URL rakt av.
    """
    return db.connect(read_only=True, role="legacy")


def run_payload(con, sql: str):
    """Kör en json_agg-payload-query och returnera parsad payload (list/dict eller [])."""
    row = con.execute(sql).fetchone()
    if not row or row[0] is None:
        return []
    return json.loads(row[0])


def fetch_all(con, period: str) -> dict:
    """Hämta allt dashboarden behöver i ett svep.

    Returnerar dict med nycklar: full_year_only_cids, ytd, personnel, companies.
    """
    per = derive_periods(period)
    targets = f"('{per['prev']}'),('{per['cur']}'),('{per['prev_fy']}')"
    ytd_sql = queries.render_query(
        queries.YTD_TOPGROUP_QUERY,
        start_period=f"{per['prev'][:4]}01",   # täck fg-årets januari → i år
        end_period=per["cur"],
        targets=targets,
    )
    return {
        "full_year_only_cids": run_payload(con, queries.FULL_YEAR_ONLY_DETECT_QUERY),
        "ytd": run_payload(con, ytd_sql),
        "personnel": run_payload(con, queries.PERSONNEL_QUERY),
        "companies": run_payload(con, queries.DIM_COMPANY_QUERY),
    }
