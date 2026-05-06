"""Hjälpare för period-aritmetik (YYYYMM-strängar)."""
from __future__ import annotations

from dataclasses import dataclass


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


def add_months(p: str, n: int) -> str:
    """'202603' + 2 → '202605'. Negativa n går bakåt över årsskiften."""
    if len(p) != 6 or not p.isdigit():
        raise ValueError(f"Ogiltigt period-format: {p!r}")
    y, m = int(p[:4]), int(p[4:])
    total = y * 12 + (m - 1) + n
    ny, nm = divmod(total, 12)
    return f"{ny}{nm + 1:02d}"


# ----- Bucket-modell för pivot-rapporten --------------------------------------


@dataclass(frozen=True)
class Bucket:
    """En kolumn i pivot-rapporten — t.ex. 'Q1 2025' eller 'LTM'."""
    key: str            # stabil id, t.ex. '2025-Q1', '2025-H1', '2025', '202503', 'LTM'
    label: str          # användarvänlig etikett
    start: str          # YYYYMM, inkluderande
    end: str            # YYYYMM, inkluderande
    granularity: str    # 'month' | 'quarter' | 'half' | 'year' | 'ltm'

    def months(self) -> list[str]:
        """Returnera alla YYYYMM-perioder i bucketen, inkluderande."""
        out, cur = [], self.start
        while cur <= self.end:
            out.append(cur)
            cur = add_months(cur, 1)
        return out


_QUARTER_MONTHS = {1: (1, 3), 2: (4, 6), 3: (7, 9), 4: (10, 12)}
_MONTH_NAMES_SV = [
    "Januari", "Februari", "Mars", "April", "Maj", "Juni",
    "Juli", "Augusti", "September", "Oktober", "November", "December",
]


def period_buckets(start: str, end: str, granularity: str) -> list[Bucket]:
    """Generera buckets från `start` till `end` med vald granularitet.

    Inkluderar bara *fullständiga* buckets (inga halv-kvartal). Hör en period
    till en bucket vars hela intervall ligger utanför start..end så hoppas den
    över, men en bucket vars startmånad är >= start och slutmånad är <= end tas med.

    granularity: 'month' | 'quarter' | 'half' | 'year'.
    """
    if granularity not in ("month", "quarter", "half", "year"):
        raise ValueError(f"Okänd granularitet: {granularity!r}")
    if start > end:
        raise ValueError(f"start ({start}) > end ({end})")

    if granularity == "month":
        return [
            Bucket(
                key=p, label=f"{_MONTH_NAMES_SV[int(p[4:]) - 1][:3]} {p[:4]}",
                start=p, end=p, granularity="month",
            )
            for p in _months_between(start, end)
        ]

    if granularity == "quarter":
        out = []
        for year in range(int(start[:4]), int(end[:4]) + 1):
            for q in range(1, 5):
                m_start, m_end = _QUARTER_MONTHS[q]
                bs = f"{year}{m_start:02d}"
                be = f"{year}{m_end:02d}"
                if bs >= start and be <= end:
                    out.append(Bucket(
                        key=f"{year}-Q{q}", label=f"Q{q} {year}",
                        start=bs, end=be, granularity="quarter",
                    ))
        return out

    if granularity == "half":
        out = []
        for year in range(int(start[:4]), int(end[:4]) + 1):
            for h, (m_start, m_end) in [(1, (1, 6)), (2, (7, 12))]:
                bs = f"{year}{m_start:02d}"
                be = f"{year}{m_end:02d}"
                if bs >= start and be <= end:
                    out.append(Bucket(
                        key=f"{year}-H{h}", label=f"H{h} {year}",
                        start=bs, end=be, granularity="half",
                    ))
        return out

    # year
    out = []
    for year in range(int(start[:4]), int(end[:4]) + 1):
        bs, be = f"{year}01", f"{year}12"
        if bs >= start and be <= end:
            out.append(Bucket(
                key=str(year), label=str(year),
                start=bs, end=be, granularity="year",
            ))
    return out


def ltm_bucket(end: str) -> Bucket:
    """LTM = senaste 12 månader fram t.o.m. `end` (inkl)."""
    if len(end) != 6 or not end.isdigit():
        raise ValueError(f"Ogiltigt period-format: {end!r}")
    start = add_months(end, -11)
    return Bucket(
        key="LTM", label="LTM",
        start=start, end=end, granularity="ltm",
    )


def ytd_bucket(end: str) -> Bucket:
    """YTD = från årets januari fram t.o.m. `end` (inkl)."""
    if len(end) != 6 or not end.isdigit():
        raise ValueError(f"Ogiltigt period-format: {end!r}")
    year = end[:4]
    start = f"{year}01"
    return Bucket(
        key=f"YTD-{year}", label=f"YTD {year}",
        start=start, end=end, granularity="ytd",
    )


def _months_between(start: str, end: str) -> list[str]:
    out, cur = [], start
    while cur <= end:
        out.append(cur)
        cur = add_months(cur, 1)
    return out
