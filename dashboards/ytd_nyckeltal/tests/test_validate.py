"""Tester för validate — MERCUR_TO_CID-mappning + RU-mappning + 2025-attach."""
import conftest  # noqa: F401  (sätter sys.path)
from validate import MERCUR_TO_CID, map_mercur_to_ru, build_validation


def test_mercur_to_cid_critical_mappings():
    """Tre kritiska namn-mappningar som lätt går sönder vid Mercur-omdöpningar."""
    assert MERCUR_TO_CID["Ålesund"] == 77
    assert MERCUR_TO_CID["Lås & Sikring AS (Elverum)"] == 148
    assert MERCUR_TO_CID["Actas A/S konsoliderat"] == 132


def test_map_mercur_to_ru_via_member_cid():
    """Mercur-namn vars cid är en SUB ska mappas till RU:n som absorberat den."""
    dash_companies = [
        {"company_id": 132, "name": "Actas", "country": "Denmark", "kind": "consolidated",
         "member_cids": [132, 81]},
        {"company_id": 77, "name": "Ålesund", "country": "Norway", "kind": "standalone",
         "member_cids": [77]},
    ]
    mapping, unmapped = map_mercur_to_ru(["Actas A/S konsoliderat", "Ålesund"], dash_companies)
    assert mapping["Actas A/S konsoliderat"]["company_id"] == 132
    assert mapping["Ålesund"]["company_id"] == 77
    assert unmapped == []


def test_build_validation_attaches_2025_facit():
    """attach_facit_to_dash ska skriva alla 6 facit-fält (rå SEK) på mappad RU."""
    dash = {"companies": [
        {"company_id": 77, "name": "Ålesund", "country": "Norway", "kind": "standalone",
         "member_cids": [77],
         "kpis": {"202604": {"sales": 6_000_000}, "202504": {"sales": 8_000_000}},
         "periods": {"202512": {"Total Sales": 20_000_000}}, "flags": []},
    ]}
    facit_2026 = {"Utfall": {"Total Sales": 6_100_000}, "Ålesund": {"Total Sales": 6_100_000}}
    facit_2025 = {"Utfall": {"Total Sales": 8_200_000}, "Ålesund": {"Total Sales": 8_200_000}}
    val = build_validation(dash, fyo=[], facit_2026=facit_2026, facit_2025=facit_2025)
    f = dash["companies"][0]["facit"]
    assert set(f) == {"ts_facit", "ts_wh", "ts_diff_pct",
                      "ts_2025_facit", "ts_2025_wh", "ts_2025_diff_pct"}
    assert f["ts_2025_facit"] == 8_200_000
    assert f["ts_2025_wh"] == 8_000_000     # icke-proxy → YTD apr 2025
    assert val["utfall_facit_25"]["Total Sales"] == 8_200_000
