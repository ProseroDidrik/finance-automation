"""Konfiguration för YTD-nyckeltalsdashboarden.

FX-kurser, top_group-listor och period-härledning. Allt som är "data om bygget"
(inte SQL, inte renderingslogik) bor här så build.py hålls tunn.
"""
from __future__ import annotations

# --- FX: månadssnitt mot SEK ------------------------------------------------
# Kommer egentligen från dim_exchange_rate (avg/månad). Hårdkodade här precis som
# i Cowork v13 (v13_build_pipeline.py) — dynamisk hämtning är en senare follow-up.
# Nycklar = de tre perioder dashboarden jämför: YTD apr i år, YTD apr fg år, helår fg år.
FX = {
    "202604": {"SEK": 1.0, "NOK": 0.94,  "DKK": 1.431, "EUR": 10.691},
    "202504": {"SEK": 1.0, "NOK": 0.957, "DKK": 1.497, "EUR": 11.165},
    "202512": {"SEK": 1.0, "NOK": 0.945, "DKK": 1.479, "EUR": 11.041},
}

# Default-FX för en period som saknas i FX (1:1 SEK + närmaste kända kurs).
FX_FALLBACK = {"SEK": 1.0, "NOK": 0.95, "DKK": 1.48, "EUR": 11.0}

# --- Top groups -------------------------------------------------------------
# De grupper YTD_TOPGROUP_QUERY hämtar ur dim_account_map-hierarkin.
TOP_GROUPS = [
    "Total Sales", "Total Direct Cost", "Personnel", "Consultants",
    "Other External Costs", "Premises", "Transportation", "Depreciation",
]

# Härledda KPI-rader (visas i renderarna) inkl. de beräknade (Bruttovinst/EBITDA).
DISPLAY_TOP_GROUPS = [
    "Total Sales", "Total Direct Cost", "Bruttovinst", "Personnel",
    "Consultants", "Other External Costs", "Premises", "Transportation",
    "Depreciation", "Justerad EBITDA",
]

# Koncerntotal Total Sales YTD 202604 (SEK) — warehouse self-consistency-ankare.
# 1598 efter rebase på main (per-månads-FX + P-kods-MAN-fixen 256662c tillsammans);
# var 1594 (FX utan P-kods-MAN), 1591 vid flat-FX. OBS: detta är warehouse-summan,
# INTE Mercur koncern-facit (1554) — skillnaden ~2,8% är koncern-elimineringar
# (pre-existerande, ej FX).
EXPECTED_KONCERN_SALES_202604_MSEK = 1598
KONCERN_SALES_TOLERANCE = 0.02  # ±2 %

DEFAULT_PERIOD = "202604"


def derive_periods(period: str) -> dict[str, str]:
    """Härled de tre jämförelseperioderna ur --period (YYYYMM).

    Returns dict med:
        cur      = perioden själv (YTD i år), t.ex. 202604
        prev     = samma månad fg år (YTD fg år), t.ex. 202504
        prev_fy  = helår fg år (proxy för full-year-only-bolag), t.ex. 202512
    """
    year, mm = int(period[:4]), period[4:6]
    return {
        "cur": period,
        "prev": f"{year - 1}{mm}",
        "prev_fy": f"{year - 1}12",
    }


def fx_for(period: str) -> dict[str, float]:
    """FX-rad för en period, med fallback om perioden saknas i FX-tabellen."""
    return FX.get(period, FX_FALLBACK)
