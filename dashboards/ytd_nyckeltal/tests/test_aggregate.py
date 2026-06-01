"""Tester för aggregate.build_dashboard_data — RU-byggande + proxy-flaggning."""
import json
from pathlib import Path

import conftest  # noqa: F401  (sätter sys.path)
from aggregate import build_dashboard_data

FIX = json.loads((Path(__file__).parent / "fixtures" / "sample_ytd.json").read_text(encoding="utf-8"))


def _build(full_year_only_cids):
    return build_dashboard_data(FIX["ytd"], FIX["companies"], FIX["personnel"],
                                FIX["monthly_rates"], full_year_only_cids)


def _ru(dash, cid):
    return next(c for c in dash["companies"] if c["company_id"] == cid)


def test_actas_consolidated_not_proxy_after_saftver():
    """Efter SAFT_VER (cid 81 EJ i full_year_only): Actas-RU är consolidated men EJ proxy."""
    dash = _build(full_year_only_cids=[])
    actas = _ru(dash, 132)
    assert "REPORTING_UNIT_CONS" in actas["flags"]
    assert "FULL_YEAR_PROXY_2025" not in actas["flags"]
    # consolidated absorberar subben 81
    assert set(actas["member_cids"]) == {132, 81}
    # riktig YoY finns (202504-baslinje, inte nullad)
    assert actas["delta"]["sales_pct"] is not None


def test_proxy_flag_when_member_full_year_only():
    """Om cid 81 vore full_year_only → Actas-RU flaggas proxy och financial-delta nullas."""
    dash = _build(full_year_only_cids=[81])
    actas = _ru(dash, 132)
    assert "FULL_YEAR_PROXY_2025" in actas["flags"]
    assert actas["delta"]["sales_pct"] is None


def test_sales_per_manads_fx_differencing():
    """Ålesund (NOK, ytd-källa) 202604: YTD-saldon differentieras till månadsrörelse
    och varje månad FX:as med SIN månadskurs (matchar Mercurs konvertering).

    YTD-snapshots 202601..202604 = -1.5/-3.2/-4.9/-6.63448 MNOK → rörelser
    -1.5/-1.7/-1.7/-1.73448 × kurser 0.91545/0.93951/0.96383/0.98210 = 6 312 285.8 SEK.
    En naiv enkurs (0.94 på hela YTD) gav 6 236 411 — det är just FX-buggen vi fixar.
    """
    dash = _build(full_year_only_cids=[])
    aalesund = _ru(dash, 77)
    assert abs(aalesund["kpis"]["202604"]["sales"] - 6_312_285.808) < 1.0
    # och skiljer sig MÄRKBART från den gamla enkurs-beräkningen (regressionsvakt)
    assert abs(aalesund["kpis"]["202604"]["sales"] - 6_634_480 * 0.94) > 50_000


def test_monthly_source_fx_direct():
    """Actas A/S (DKK) 202604 enkel ytd-snapshot → abs(YTD-saldo × månadskurs)."""
    dash = _build(full_year_only_cids=[])
    actas = _ru(dash, 132)  # consolidated RU absorberar subben 81
    assert abs(actas["kpis"]["202604"]["sales"] - 52_000_000 * 1.44990) < 1.0
