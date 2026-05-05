"""Hjälpare för period-aritmetik (YYYYMM-strängar)."""
from __future__ import annotations


def prev_period(p: str) -> str:
    """'202603' -> '202602', '202601' -> '202512'."""
    if len(p) != 6 or not p.isdigit():
        raise ValueError(f"Ogiltigt period-format: {p!r}")
    y, m = int(p[:4]), int(p[4:])
    if m == 1:
        return f"{y - 1}12"
    return f"{y}{m - 1:02d}"


def year_start(p: str) -> str:
    """'202603' -> '202601'."""
    if len(p) != 6 or not p.isdigit():
        raise ValueError(f"Ogiltigt period-format: {p!r}")
    return p[:4] + "01"
