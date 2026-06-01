"""Tester för aggregate.build_dashboard_data — RU-byggande + proxy-flaggning."""
import json
from pathlib import Path

import conftest  # noqa: F401  (sätter sys.path)
from aggregate import build_dashboard_data

FIX = json.loads((Path(__file__).parent / "fixtures" / "sample_ytd.json").read_text(encoding="utf-8"))


def _build(full_year_only_cids):
    return build_dashboard_data(FIX["ytd"], FIX["companies"], FIX["personnel"],
                                FIX["fx_rates"], full_year_only_cids)


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


def test_sales_fx_per_month_with_carry_forward():
    """Ålesund (NOK) 202604: varje månadsrörelse × sin egen snittkurs, abs().

    Månader: 202602 −2,0M @0.90, 202603 −2,0M @0.95, 202604 −2,634,480 @ saknad
    kurs → carry-forward 0.95. Bevisar både per-månads-differentiering och
    carry-forward (skiljer sig från en enda YTD-kurs)."""
    dash = _build(full_year_only_cids=[])
    aalesund = _ru(dash, 77)
    expected = abs(-2_000_000 * 0.90 + -2_000_000 * 0.95 + -2_634_480 * 0.95)
    assert abs(aalesund["kpis"]["202604"]["sales"] - expected) < 1.0
    # Sanity: skiljer sig från naiv enkel-kurs (skulle bli 6,634,480 × 0.95)
    assert abs(aalesund["kpis"]["202604"]["sales"] - 6_634_480 * 0.95) > 1.0


def test_sek_company_unchanged_rate_1():
    """SEK-bolag (ej i fx_rates) → kurs 1.0, lokalbelopp == SEK."""
    from aggregate import _make_fx_resolver
    rate_of, _ = _make_fx_resolver(FIX["fx_rates"])
    assert rate_of("SEK", "202604") == 1.0
