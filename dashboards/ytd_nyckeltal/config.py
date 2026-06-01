"""Konfiguration för YTD-nyckeltalsdashboarden.

Top_group-listor och period-härledning. Allt som är "data om bygget" (inte SQL,
inte renderingslogik) bor här så build.py hålls tunn.

FX bor INTE här längre: månadsvisa kurser laddas ur `_params/Valutakurser.xlsx`
av `fx.py` och appliceras PER MÅNAD (se aggregate._ytd_sek_by_key). Den gamla
hårdkodade enkurs-tabellen (NOK 202604=0.94) gav ~1,4 % FX-fel på NOK-bolag.
"""
from __future__ import annotations

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

# Warehouse koncerntotal Total Sales YTD 202604 (SEK) — datalager-tripwire.
# v1.7: 1591→1597 efter per-månads-FX (NOK-bolag konverteras nu med rätt månadskurs,
# ~1,4 % högre → +6 MSEK på koncernen). OBS: detta är WAREHOUSE-summan (sum-of-parts),
# inte Mercurs konsoliderade koncern (~1554 MSEK, ~−2,8 % lägre). Den diffen är
# strukturell och INTE FX (fanns redan i v13/v14) — sannolikt intercompany-
# elimineringar som en rak summa inte replikerar (ej fullt verifierat). Ankaret
# fångar datalager-regressioner mot warehouse-summan, inte den koncerndiffen.
EXPECTED_KONCERN_SALES_202604_MSEK = 1597
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
