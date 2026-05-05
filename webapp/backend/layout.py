"""Layout-config: ordning på storgrupperna i P&L-rapporten.

Läser webapp/config/pnl_layout.yaml och post-processerar rapporterade rader
genom att prefixa sort_path med ett ordnings-index — så frontend kan fortsätta
sortera på sort_path och få Mercur-ordningen.
"""
from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
LAYOUT_PATH = REPO / "webapp" / "config" / "pnl_layout.yaml"


def load_storgrupp_order() -> dict[str, int]:
    """Returnerar account_id → ordnings-index (0-baserat). Storgrupper som
    saknas i config får index 999."""
    with LAYOUT_PATH.open(encoding="utf-8") as f:
        order = yaml.safe_load(f).get("storgrupp_order", []) or []
    return {sg: i for i, sg in enumerate(order)}


def _storgrupp_of(sort_path: str) -> str:
    """Plocka ut storgruppens account_id från en rads sort_path.

    sort_path-format från SQL: 'P&L/<storgrupp>/<grupp>/<gruppkonto>/<bolagskonto>'
    """
    parts = sort_path.split("/")
    return parts[1] if len(parts) >= 2 else ""


def reorder_rows(rows: list[dict]) -> list[dict]:
    """Sortera rader efter storgrupp-ordning (Mercur), behåll sort_path inom."""
    order = load_storgrupp_order()
    default_idx = len(order) + 1

    def key(r: dict) -> tuple[int, str]:
        sg = _storgrupp_of(r.get("sort_path", ""))
        return (order.get(sg, default_idx), r.get("sort_path", ""))

    sorted_rows = sorted(rows, key=key)
    # Skriv om sort_path så frontend kan fortsätta sortera på den
    out = []
    for r in sorted_rows:
        sg = _storgrupp_of(r.get("sort_path", ""))
        idx = order.get(sg, default_idx)
        new_path = f"{idx:03d}|{r['sort_path']}"
        out.append({**r, "sort_path": new_path})
    return out
