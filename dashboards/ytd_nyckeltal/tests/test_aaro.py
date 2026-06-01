"""Tester för aaro — label-parsning + AARO_DATA-byggande (hermetiska, ingen DB/xlsx)."""
import conftest  # noqa: F401  (sätter sys.path)
from mercur import _parse_aaro_label
from aaro import build_aaro_classification


def test_parse_aaro_label():
    assert _parse_aaro_label('  Sales 3010 Sales, external') == ('Sales', '3010', 'Sales, external')
    # account_id med mellanslag — icke-girig (.+?) expanderar till 4-siffer-koden
    assert _parse_aaro_label('  Other Sales 3990 Other operating income') == \
        ('Other Sales', '3990', 'Other operating income')
    assert _parse_aaro_label('  OTH_SALES_REA_EXCHANGE 3960 Realized exchange gains') == \
        ('OTH_SALES_REA_EXCHANGE', '3960', 'Realized exchange gains')
    assert _parse_aaro_label('Försäljning') is None          # top_group-rad (ingen kod)


def test_build_aaro_classification_shape_and_diff():
    aaro_2026 = [
        {'top_group': 'Försäljning', 'account_id': 'Sales', 'aaro_code': '3010',
         'desc': 'Sales, external', 'utfall': 1_000_000.0},
        {'top_group': 'Materialkostnader', 'account_id': 'COGS', 'aaro_code': '4010',
         'desc': 'COGS', 'utfall': -400_000.0},
    ]
    aaro_2025 = [
        {'top_group': 'Försäljning', 'account_id': 'Sales', 'aaro_code': '3010',
         'desc': 'Sales, external', 'utfall': 900_000.0},
        {'top_group': 'Materialkostnader', 'account_id': 'COGS', 'aaro_code': '4010',
         'desc': 'COGS', 'utfall': -350_000.0},
    ]
    wh = {('Sales', '202604'): 950_000.0, ('Sales', '202504'): 900_000.0,
          ('COGS', '202604'): 400_000.0}   # COGS 202504 saknas → 0
    recs = build_aaro_classification(aaro_2026, aaro_2025, wh, '202604', '202504')

    assert len(recs) == 2
    keys = {'top_group', 'account_id', 'aaro_code', 'desc',
            'facit_utfall', 'warehouse_total', 'diff', 'diff_pct',
            'facit_utfall_25', 'warehouse_total_25', 'diff_25', 'diff_pct_25'}
    assert set(recs[0]) == keys

    sales = recs[0]
    assert sales['facit_utfall'] == 1_000_000          # abs
    assert sales['warehouse_total'] == 950_000
    assert sales['diff'] == 50_000
    assert abs(sales['diff_pct'] - 0.05) < 1e-9
    # 2025 exakt match → diff 0
    assert sales['ts_2025_facit' if False else 'facit_utfall_25'] == 900_000
    assert sales['diff_25'] == 0

    cogs = recs[1]
    assert cogs['facit_utfall'] == 400_000             # abs av -400k
    assert cogs['warehouse_total_25'] == 0             # saknades i wh → 0
    assert cogs['diff_pct_25'] is not None             # facit 350k > 1000 → beräknas


def test_build_aaro_classification_small_facit_no_pct():
    """facit ≤ 1000 SEK → diff_pct = None (undvik brus på minimala konton)."""
    a26 = [{'top_group': 'X', 'account_id': 'Tiny', 'aaro_code': '9999',
            'desc': 'd', 'utfall': 500.0}]
    a25 = [{'top_group': 'X', 'account_id': 'Tiny', 'aaro_code': '9999',
            'desc': 'd', 'utfall': 0.0}]
    recs = build_aaro_classification(a26, a25, {}, '202604', '202504')
    assert recs[0]['diff_pct'] is None
    assert recs[0]['diff_pct_25'] is None
