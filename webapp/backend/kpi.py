"""KPI-evaluator för P&L-rapporten.

Läser pnl_kpis.yaml och beräknar varje KPI utifrån report_pnl-utdata.
Sign-konvention: KPI-formler räknar på POST-FLIP-värden
(display_amount = -raw_amount). Se YAML-headern för förklaring.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
KPI_PATH = REPO / "webapp" / "config" / "pnl_kpis.yaml"

# kpi:<id> | <account_id> (kan innehålla space, &, parentes, _, +, etc.)
_TOKEN_RE = re.compile(r'kpi:[A-Za-z_][\w]*|"[^"]+"|[A-Za-z_][\w &/+()\-]*')


def load_kpis() -> list[dict]:
    with KPI_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)["kpis"]


def _flip(v: float | None) -> float | None:
    """Sign-flip för P&L-presentation. None bevaras."""
    return None if v is None else -v


def _resolve_node(account_id: str, by_account: dict, by_kpi: dict, col: str) -> float | None:
    """Slå upp display-värdet (post-flip) för ett account_id eller kpi:<id>."""
    if account_id.startswith("kpi:"):
        kpi_id = account_id.split(":", 1)[1]
        return by_kpi.get(kpi_id, {}).get(col)
    raw = by_account.get(account_id, {}).get(col)
    return _flip(raw)


def _eval_formula(formula: str, by_account: dict, by_kpi: dict, col: str) -> float | None:
    """Resolva formel-strängen genom token-substitution + eval."""
    expr = formula
    # Sortera tokens längst-först så "kpi:total_sales" inte plockas som "kpi" + ":total_sales"
    tokens = sorted(set(_TOKEN_RE.findall(expr)), key=len, reverse=True)
    sub = {}
    has_division = "/" in expr
    for tok in tokens:
        clean = tok.strip().strip('"')
        if clean in {"+", "-", "*", "/"}:
            continue
        val = _resolve_node(clean, by_account, by_kpi, col)
        # Vid summa/differens: tom nod = 0 (ingen rörelse på det kontot).
        # Vid division: None bevaras → KPI blir None.
        if val is None and not has_division:
            sub[tok] = 0.0
        else:
            sub[tok] = val

    # Om division och någon term är None → None.
    if any(v is None for v in sub.values()):
        return None

    # Bygg upp expression med substituerade värden
    out = expr
    for tok, val in sorted(sub.items(), key=lambda kv: -len(kv[0])):
        out = out.replace(tok, f"({val})")

    try:
        return float(eval(out, {"__builtins__": {}}, {}))
    except (SyntaxError, NameError, ZeroDivisionError):
        return None


def compute_kpis(report_rows: list[dict]) -> dict[str, dict]:
    """
    Returnerar dict[kpi_id] = {
        'label_sv', 'label_en', 'anchor', 'format', 'emphasis',
        'amount_month', 'amount_ytd'  # POST-FLIP-värden, redo för display
    }
    """
    # Index P&L-noder per account_id
    by_account: dict[str, dict] = {}
    for r in report_rows:
        if r.get("is_aggregated"):
            by_account[r["account_id"]] = {
                "amount_month": r.get("amount_month"),
                "amount_ytd": r.get("amount_ytd"),
            }

    by_kpi: dict[str, dict] = {}
    for kpi in load_kpis():
        kid = kpi["id"]
        m = _eval_formula(kpi["formula"], by_account, by_kpi, "amount_month")
        y = _eval_formula(kpi["formula"], by_account, by_kpi, "amount_ytd")
        by_kpi[kid] = {
            "label_sv": kpi["label_sv"],
            "label_en": kpi["label_en"],
            "anchor": kpi["anchor"],
            "format": kpi.get("format", "currency"),
            "emphasis": kpi.get("emphasis", "subtotal"),
            "amount_month": m,
            "amount_ytd": y,
        }
    return by_kpi
