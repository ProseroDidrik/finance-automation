"""Bygg reporting units från YTD-data + dim_company.

Användning från SKILL-flödet:
    from build_ru_aggregat import build_dashboard_data
    dash = build_dashboard_data(ytd, companies, personnel, fx)
    json.dump(dash, open('dashboard_data.json', 'w'))

Reporting unit-logik:
- consolidated bolag → reporting unit, absorberar alla subs där parent_id == cid
- standalone → reporting unit för sig
- sub/decommissioned_sub utan consolidated parent → "orphan_sub" reporting unit

Aggregering använder abs() per (cid, period, top_group) för att vara robust mot tecken-konventionsavvikelser.
"""
import json
from collections import defaultdict

import fx as fxmod

# v1.5: full_year_only-mängden DETEKTERAS DYNAMISKT i körtid via
# sql_queries.FULL_YEAR_ONLY_DETECT_QUERY (bolag med bara helårs-SAFT 202512 för
# 2025) och skickas in i build_dashboard_data som `full_year_only_cids`. Tidigare
# (v1.4) var listan hårdkodad här — den ströks för att slippa underhåll när SAFT
# laddas om. För dessa bolag finns ingen YTD-apr-2025-baslinje: financial YoY mot
# 202504 är meningslös, de flaggas FULL_YEAR_PROXY_2025 och jämförs 202512-helår
# mot Mercurs helår i stället.


def build_reporting_units(companies):
    """Returnera dict {ru_id: {reporting_cid, name, country, kind, currency, member_cids}}."""
    cid_to_co = {c['company_id']: c for c in companies}
    parent_to_children = {}
    for c in companies:
        if c.get('parent_id'):
            parent_to_children.setdefault(c['parent_id'], []).append(c['company_id'])

    absorbed = set()
    ru_meta = {}

    # First pass: consolidated units absorb subs
    for c in companies:
        if c['kind'] == 'consolidated':
            kids = parent_to_children.get(c['company_id'], [])
            ru_meta[c['company_id']] = {
                'reporting_cid': c['company_id'], 'name': c['name'],
                'country': c['country'], 'kind': 'consolidated',
                'currency': c['currency'],
                'member_cids': [c['company_id']] + kids,
            }
            for k in kids:
                absorbed.add(k)

    # Second pass: standalone + orphan subs
    for c in companies:
        if c['kind'] == 'standalone':
            ru_meta[c['company_id']] = {
                'reporting_cid': c['company_id'], 'name': c['name'],
                'country': c['country'], 'kind': 'standalone',
                'currency': c['currency'],
                'member_cids': [c['company_id']],
            }
        elif c['kind'] in ('sub', 'decommissioned_sub') and c['company_id'] not in absorbed:
            ru_meta[c['company_id']] = {
                'reporting_cid': c['company_id'], 'name': c['name'],
                'country': c['country'], 'kind': 'orphan_sub',
                'currency': c['currency'],
                'member_cids': [c['company_id']],
            }

    return ru_meta, cid_to_co, parent_to_children


def _ytd_sek_by_key(ytd, monthly_rates):
    """Konvertera månadsgrain-rader → {(cid, target_period, top_group): abs(YTD-SEK)}.

    Per-månads-FX (matchar Mercur): ytd-rader (SAFT/SIE/SIE_VER) differentieras till
    månadsrörelse (mån − föreg. mån inom årsfönstret) och multipliceras med den
    månadens kurs; monthly-rader (PSALDO/IMP + MAN/IMP_ADJ) FX:as direkt. abs()
    appliceras på YTD-SEK-summan (teckenrobust, som tidigare).
    """
    groups = defaultdict(list)  # key → [(month, period_type, currency, amount_local)]
    for r in ytd:
        key = (r['company_id'], r['target_period'], r['top_group'])
        groups[key].append((r['month'], r['period_type'], r['currency'], r['amount_local'] or 0))

    out = {}
    for key, items in groups.items():
        sek = 0.0
        # ytd-rader: differentiera kumulativt saldo → månadsrörelse (sorterat, prev=0).
        # Varje grupp = ETT target = ETT årsfönster, så prev nollas naturligt per år.
        prev = 0.0
        for month, _pt, cur, amt in sorted((m, pt, c, a) for (m, pt, c, a) in items if pt == 'ytd'):
            sek += (amt - prev) * fxmod.rate(monthly_rates, month, cur)
            prev = amt
        # monthly-rader: rörelsen FX:as rakt av.
        for month, pt, cur, amt in items:
            if pt == 'monthly':
                sek += amt * fxmod.rate(monthly_rates, month, cur)
        out[key] = abs(sek)
    return out


def build_dashboard_data(ytd, companies, personnel, monthly_rates, full_year_only_cids):
    """Bygg dashboard_data.json-payload från rådata.

    Args:
        ytd: list of {target_period, company_id, currency, top_group, month, period_type,
             amount_local} — månadsgrain (v1.7). En rad per (target, bolag, top_group,
             månad). period_type='ytd' → amount_local är YTD-saldot den månaden
             (differentieras), 'monthly' → månadsrörelse. För full_year_only-bolag
             saknas 202504-fönstret; deras 202512 används som proxy.
        companies: dim_company list with parent_id
        personnel: per-cid personnel data
        monthly_rates: dict {period(YYYYMM): {currency: rate_to_SEK}} — månadsvis
             genomsnittskurs (fx.load_monthly_rates). FX appliceras PER MÅNAD.
        full_year_only_cids: iterable av company_ids som saknar månadsvis SAFT 2025
             (bara helårs-SAFT 202512). Detekteras dynamiskt via
             sql_queries.FULL_YEAR_ONLY_DETECT_QUERY — flaggas FULL_YEAR_PROXY_2025.

    Returns:
        dict {'companies': [...], 'meta': {...}}
    """
    full_year_only = set(full_year_only_cids)
    ru_meta, cid_to_co, _ = build_reporting_units(companies)
    cid_to_ru = {m: ru_id for ru_id, ru in ru_meta.items() for m in ru['member_cids']}

    # v1.4: ingen journal_saft-2025-syntes längre (fabricerade siffror, bara ~6%
    # inläst — se pitfall #11). full_year_only-bolag får istället helårsproxy
    # från sin egen 202512-SAFT (redan i `ytd`) + flaggan FULL_YEAR_PROXY_2025.
    sek_by_key = _ytd_sek_by_key(ytd, monthly_rates)

    # Aggregate per RU (abs() redan applicerad i _ytd_sek_by_key, teckenrobust)
    ru_data = {ru_id: {**ru, 'periods': {'202504': {}, '202604': {}, '202512': {}}}
               for ru_id, ru in ru_meta.items()}

    for (cid, period, tg), amt_sek in sek_by_key.items():
        ru_id = cid_to_ru.get(cid)
        if ru_id is None:
            continue
        ru_data[ru_id]['periods'][period][tg] = ru_data[ru_id]['periods'][period].get(tg, 0) + amt_sek

    # Personnel
    pers_by_cid = {p['company_id']: p for p in personnel}
    for ru_id, ru in ru_data.items():
        f25 = f26 = fd = 0
        s25 = s26 = sd = False
        hc25 = hc26 = hcd = h26 = l26 = h25 = l25 = 0
        for cid in ru['member_cids']:
            p = pers_by_cid.get(cid)
            if not p:
                continue
            if p.get('fte_apr_2025'): f25 += p['fte_apr_2025']; s25 = True
            if p.get('fte_apr_2026'): f26 += p['fte_apr_2026']; s26 = True
            if p.get('fte_dec_2025'): fd += p['fte_dec_2025']; sd = True
            hc25 += p.get('hc_apr_2025') or 0
            hc26 += p.get('hc_apr_2026') or 0
            hcd += p.get('hc_dec_2025') or 0
            h26 += p.get('hires_2026') or 0
            l26 += p.get('leavers_2026') or 0
            h25 += p.get('hires_2025_ytd') or 0
            l25 += p.get('leavers_2025_ytd') or 0
        ru['fte_apr_2025'] = f25 if s25 else None
        ru['fte_apr_2026'] = f26 if s26 else None
        ru['fte_dec_2025'] = fd if sd else None
        ru['hc_apr_2025'] = hc25 or None
        ru['hc_apr_2026'] = hc26 or None
        ru['hc_dec_2025'] = hcd or None
        ru['hires_2026'] = h26 or None
        ru['leavers_2026'] = l26 or None
        ru['hires_2025_ytd'] = h25 or None
        ru['leavers_2025_ytd'] = l25 or None

    # KPIs + delta
    def sd_(a, b):
        return None if not b else a / b

    for ru_id, ru in ru_data.items():
        ru['kpis'] = {}
        for period in ['202504', '202604', '202512']:
            p = ru['periods'][period]
            s = p.get('Total Sales', 0)
            td = p.get('Total Direct Cost', 0)
            pe = p.get('Personnel', 0)
            co = p.get('Consultants', 0)
            oe = p.get('Other External Costs', 0)
            pr = p.get('Premises', 0)
            tr = p.get('Transportation', 0)
            de = p.get('Depreciation', 0)
            gp = s - td
            ox = pe + co + oe + pr + tr
            ru['kpis'][period] = {
                'sales': s, 'tdc': td, 'gross_profit': gp,
                'gross_margin': sd_(gp, s),
                'personnel': pe, 'consultants': co, 'other_ext': oe,
                'premises': pr, 'transport': tr, 'depreciation': de,
                'opex': ox, 'ebitda': gp - ox,
                'bv_per_pkr': sd_(gp, pe), 'pers_pct': sd_(pe, s),
            }
        fte26 = ru.get('fte_apr_2026')
        if fte26 and fte26 > 0:
            ru['sales_per_fte'] = ru['kpis']['202512'].get('sales', 0) / fte26
            ru['gp_per_fte'] = ru['kpis']['202512'].get('gross_profit', 0) / fte26
        else:
            ru['sales_per_fte'] = None
            ru['gp_per_fte'] = None

        k25 = ru['kpis']['202504']
        k26 = ru['kpis']['202604']
        fte_delta = (ru.get('fte_apr_2026') or 0) - (ru.get('fte_apr_2025') or 0)
        is_proxy = any(m in full_year_only for m in ru['member_cids'])
        if is_proxy:
            # Ingen YTD-apr-2025-baslinje → financial YoY mot 202504 är meningslös.
            # Nulla financial-deltan; jämför 202512-helår mot Mercurs helår i dashboarden.
            # FTE-delta behålls BARA om apr-2025-snapshot finns — annars blir det
            # fte_apr_2026 − 0 = hela 2026-headcounten felaktigt visad som nyanställd
            # (några full_year_only-bolag saknar apr-2025-snapshot, t.ex. cid 52).
            ru['delta'] = {
                'sales_abs': None, 'sales_pct': None,
                'personnel_abs': None, 'personnel_pct': None,
                'consultants_abs': None, 'consultants_pct': None,
                'other_ext_abs': None, 'other_ext_pct': None,
                'gp_abs': None, 'gp_pct': None,
                'fte_delta': (fte_delta if ru.get('fte_apr_2025') else None),
            }
        else:
            ru['delta'] = {
                'sales_abs': k26['sales'] - k25['sales'],
                'sales_pct': sd_(k26['sales'] - k25['sales'], k25['sales']),
                'personnel_abs': k26['personnel'] - k25['personnel'],
                'personnel_pct': sd_(k26['personnel'] - k25['personnel'], k25['personnel']),
                'consultants_abs': k26['consultants'] - k25['consultants'],
                'consultants_pct': sd_(k26['consultants'] - k25['consultants'], k25['consultants']),
                'other_ext_abs': k26['other_ext'] - k25['other_ext'],
                'other_ext_pct': sd_(k26['other_ext'] - k25['other_ext'], k25['other_ext']),
                'gp_abs': k26['gross_profit'] - k25['gross_profit'],
                'gp_pct': sd_(k26['gross_profit'] - k25['gross_profit'], k25['gross_profit']),
                'fte_delta': fte_delta,
            }

        flags = []
        if is_proxy: flags.append('FULL_YEAR_PROXY_2025')
        if ru['kind'] == 'consolidated': flags.append('REPORTING_UNIT_CONS')
        if not ru.get('fte_apr_2026'): flags.append('NO_FTE')
        if not k26.get('sales') and not k25.get('sales'): flags.append('NO_SALES_DATA')
        ru['flags'] = flags

    # Filter out empty RUs
    companies_out = []
    for ru_id, ru in ru_data.items():
        has_data = any(any(v for v in ru['periods'][p].values()) for p in ru['periods'])
        if has_data or ru.get('fte_apr_2026'):
            out = dict(ru)
            out['company_id'] = ru_id
            if 'reporting_cid' in out:
                del out['reporting_cid']
            companies_out.append(out)

    return {
        'companies': companies_out,
        'meta': {
            'periods': ['202504', '202604', '202512'],
            'aggregation': 'per RU with abs() — per-månads-FX (v1.7)',
            'fx_assumptions': monthly_rates,
        },
    }
