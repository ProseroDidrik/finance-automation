"""Validera warehouse-data mot Mercur Resultaträkning-facit (2026 OCH 2025).

Utökad från fte-ytd v1.6:s validate_facit.py, som bara byggde 2026-jämförelsen.
Tillägg här:
  1. 2025-kolumnen (Mercur 'Utfall fg. år', Col 111+) — parsas av mercur.py.
  2. Koncerntotaler för båda år (utfall_facit / utfall_wh / utfall_facit_25).
  3. attach_facit_to_dash: skriver c['facit'] på varje RU i RÅ SEK
     ({ts_facit, ts_wh, ts_diff_pct, ts_2025_facit, ts_2025_wh, ts_2025_diff_pct}) —
     det är kontraktet både render_html.py (facitDot) och render_xlsx.py läser.

Output-shapen är DIKTERAD av renderarna, inte fritt designad. Ändra inte utan att
kolla att build_xlsx/update_html fortf. hittar sina fält.
"""
from __future__ import annotations

import re

# Manuell mappning mercur-namn → BolagsID (från fte-ytd references/mercur_mapping.md).
MERCUR_TO_CID = {
    # Prosero centrala
    'Prosero Security AS': 52, 'Prosero Security AB': 51, 'Prosero Security GmbH': 187,
    'Prosero Security Group AB': 51, 'Prosero Security Holding AB': 53, 'Prosero Secuity OY': 145,
    'Prosero Doorway': 162, 'Prosero Security Denmark A/S': None,
    # Konsoliderade
    'Axlås & Begelås konsoliderad': 101, 'Passera konsoliderat': 160,
    'OpenUp & Montageservice Konsoliderat': 154, 'Dalek & Sotenäs Konsoliderat': 22,
    'Dala Lås konsoliderat': 24, 'Lås & Nyckel i Gävle konsoliderat': 27,
    'Lås & Nyckel Gävle konsoliderat': 27,
    'Säkerhetsteknik konsoliderat': 29, 'Säkerhetsteknik & All-Round & ADS & SKT Konsoliderat': 29,
    'Sickla Lås gruppen': 138, 'Sickla Låsgruppen konsoliderat': 138,
    'Norsk Brannvern konsoliderat': 140, 'Norsk Brannvern konsoliderad': 140,
    'Buysec-Buytec konsoliderat': 174, 'Sikring Nord konsoliderat': 192,
    'Assistent Partner konsoliderat': 203, 'Assistent Partner konsoliderad': 203,
    'Brann og Sikrings. & Lås og Beslag konsoliderat': 206,
    'Brann og Sikringsservice & Lås og Beslag konsoliderad': 206,
    'Norrskydd konsoliderat': 213, 'Norrskydd konsoliderad': 213,
    'Safeexit konsoliderat': 225, 'Safexit konsoliderad': 225,
    'Sundsvall konsoliderat': 227, 'Romerike konsoliderat': 228,
    'Kungälv & Säkerhetspartner konsoliderat': 241,
    'Actas konsoliderad': 132, 'Actas A/S konsoliderat': 132,
    # Tyska
    'Weckbacher Sicherheitssysteme GmbH': 220, 'Franz Mittermeier GmbH': 231,
    'H+W Mechatronik GmbH': 246, 'Goldfunk Sicherheitstechnik GmbH': 245, 'Bofferding GmbH': 188,
    # NO med mappningsfix
    'Låsservice Stavanger AS': 233, 'Ålesund': 77,
    'Lås & Sikring AS (Elverum)': 148, 'Lås & Sikring AS Namsos': 217,
    'Aker Lås og Nøkkel AS': 78, 'Asker Lås': 158,
    'Hemer Lås & Dørtelefon AS': 157, 'Nordland Lås & Sikkerhet AS': 165,
    'Låsesmeden Finnsnes AS': 171, 'Lofoten låsservice AS': 200,
    'JM Lukko - Ja Turvatekniikka Oy': 221, 'WEO Lås & Sikkerhets AS': 244,
    'THV Teleja Hälytysvalvonta Oy': 182, 'Meri Lapin Lukituspalvelu Oy': 195,
    'Turvatalo - Tapiolan Yleishuolto Oy': 153,
    # SE med längre namn
    'Tele & Säkerhetstjänst i Skara AB': 6, 'Låssmeden Sven Alexandersson AB': 14,
    'Cadsafe Brandservice AB': 21, 'Creab säkerhet AB': 105, 'Exista Säkerhet AB': 151,
    'El & Fastighetsdrift Stockholm AB': 164, 'Safetytech i Väst AB': 23,
    'Hässleholms Låssmed AB': 93, 'Larmatic Alarm AB': 110,
    'Södra Vägens Låsservice AB': 152, 'Norrbottens Larmkonsult AB': 172,
    'Uppsala Säkerhetsteknik AB': 180, 'Låssmeden KanLås AB': 186,
    'Lås-Arne Malmström AB': 197,
    # DK
    'SIKOM Danmark A/S': 216,
    # Skip
    'Utfall fg. år': None, 'Elimineringsbolag Sverige': None,
    'Elimineringsbolag Norge': None, 'Elimineringsbolag Finland': None,
    'Elimineringsbolag Central': None,
}

TOP_GROUPS = ['Total Sales', 'Total Direct Cost', 'Bruttovinst', 'Personnel',
              'Consultants', 'Other External Costs', 'Premises', 'Transportation',
              'Depreciation', 'Justerad EBITDA']

TG_KPI = {'Total Sales': 'sales', 'Total Direct Cost': 'tdc', 'Personnel': 'personnel',
          'Consultants': 'consultants', 'Other External Costs': 'other_ext',
          'Premises': 'premises', 'Transportation': 'transport',
          'Depreciation': 'depreciation', 'Bruttovinst': 'gross_profit',
          'Justerad EBITDA': 'ebitda'}


def norm(s):
    s = s.lower()
    s = re.sub(r'\b(ab|as|a/s|oy|gmbh|aps|konsoliderad|konsoliderat|konsolidiert|aktiebolag|sicherheitssysteme|sicherheitstechnik|inkl|group)\b', ' ', s)
    s = re.sub(r'[^\w\s]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def map_mercur_to_ru(facit_names, dash_companies):
    """Mappa mercur-namn → dash-RU (varje RU = en post i dash['companies']).

    Returnerar (mapping {mercur_name: ru_dict}, unmapped list). RU-dicten är en
    REFERENS in i dash['companies'] — attach_facit_to_dash muterar den direkt.
    """
    cid_to_ru = {m: ru for ru in dash_companies for m in (ru.get('member_cids') or [])}
    for ru in dash_companies:                       # RU-id självt ska också träffa
        cid_to_ru.setdefault(ru['company_id'], ru)
    ru_by_norm = {}
    for ru in dash_companies:
        ru_by_norm.setdefault(norm(ru['name']), []).append(ru)

    mapping, unmapped = {}, []
    for mname in facit_names:
        if mname == 'Utfall':
            continue
        if mname in MERCUR_TO_CID:
            cid = MERCUR_TO_CID[mname]
            if cid is None:
                unmapped.append({'name': mname, 'reason': 'manual_skip'})
                continue
            if cid in cid_to_ru:
                mapping[mname] = cid_to_ru[cid]
            else:
                unmapped.append({'name': mname, 'reason': f'cid_{cid}_no_ru'})
            continue

        mn = norm(mname)
        if mn in ru_by_norm:
            picked = None
            for kind in ('standalone', 'consolidated', 'orphan_sub'):
                for ru in ru_by_norm[mn]:
                    if ru.get('kind') == kind:
                        picked = ru
                        break
                if picked:
                    break
            if picked:
                mapping[mname] = picked
                continue

        # Token-overlap fuzzy
        mn_toks = set(mn.split())
        best, bs = None, 0
        for ru in dash_companies:
            rn = norm(ru['name'])
            rn_toks = set(rn.split())
            if not rn_toks:
                continue
            overlap = len(rn_toks & mn_toks) / min(len(rn_toks), len(mn_toks))
            if (rn in mn or mn in rn) and overlap >= 0.5:
                s = min(len(rn), len(mn)) / max(len(rn), len(mn))
                if s > bs:
                    best, bs = ru, s
            elif overlap >= 1.0 and overlap > bs:
                best, bs = ru, overlap
        if best and bs >= 0.5:
            mapping[mname] = best
        else:
            unmapped.append({'name': mname, 'reason': 'no_match'})

    return mapping, unmapped


def _kpi(ru, period, kpi_key):
    """abs() av en KPI för en RU+period (0 om saknas)."""
    return abs((ru.get('kpis', {}).get(period, {}) or {}).get(kpi_key, 0) or 0)


def _wh_sales_2025(ru, fyo):
    """(wh_sales_sek, källetikett) för 2025. Full-year-only → 202512-helårsproxy."""
    is_proxy = any(m in fyo for m in (ru.get('member_cids') or []))
    if is_proxy:
        v = abs((ru.get('periods', {}).get('202512', {}) or {}).get('Total Sales', 0) or 0)
        return v, 'SAFT 202512 helår'
    return _kpi(ru, '202504', 'sales'), 'YTD apr 2025'


def _diff_pct(facit, wh):
    return (facit - wh) / facit if facit > 1000 else None


def build_validation(dash, fyo, facit_2026, facit_2025):
    """Bygg VALIDATION-payloaden + attacha c['facit'] på dash-RU:erna (rå SEK)."""
    fyo = set(fyo or [])
    dash_companies = dash['companies']
    mapping, unmapped = map_mercur_to_ru(facit_2026.keys(), dash_companies)

    # --- Koncerntotaler (rå SEK) -------------------------------------------------
    konc26 = facit_2026.get('Utfall', {})
    konc25 = facit_2025.get('Utfall', {})
    utfall_facit = {tg: abs(konc26.get(tg, 0) or 0) for tg in TOP_GROUPS}
    utfall_facit_25 = {tg: abs(konc25.get(tg, 0) or 0) for tg in TOP_GROUPS}
    utfall_wh = {tg: sum(_kpi(ru, '202604', TG_KPI[tg]) for ru in dash_companies)
                 for tg in TOP_GROUPS}

    # --- Per-RU rader + attach c['facit'] ---------------------------------------
    rows = []
    for mname, ru in mapping.items():
        fb26 = facit_2026.get(mname, {})
        fb25 = facit_2025.get(mname, {})
        row = {
            'mercur_name': mname, 'reporting_cid': ru['company_id'],
            'name': ru['name'], 'country': ru.get('country', ''),
            'kind': ru.get('kind', ''), 'member_cids': ru.get('member_cids', []),
        }
        for tg in TOP_GROUPS:
            fv = abs(fb26.get(tg, 0) or 0)
            wv = _kpi(ru, '202604', TG_KPI[tg])
            row[tg] = {'facit': round(fv), 'wh': round(wv),
                       'diff': round(fv - wv), 'diff_pct': _diff_pct(fv, wv)}
        rows.append(row)

        # attach (rå SEK) — renderarna delar själva med 1e6
        ts26_f = abs(fb26.get('Total Sales', 0) or 0)
        ts26_w = _kpi(ru, '202604', 'sales')
        ts25_f = abs(fb25.get('Total Sales', 0) or 0)
        ts25_w, _src = _wh_sales_2025(ru, fyo)
        ru['facit'] = {
            'ts_facit': round(ts26_f), 'ts_wh': round(ts26_w),
            'ts_diff_pct': _diff_pct(ts26_f, ts26_w),
            'ts_2025_facit': round(ts25_f), 'ts_2025_wh': round(ts25_w),
            'ts_2025_diff_pct': _diff_pct(ts25_f, ts25_w),
        }

    return {
        'rows': rows,
        'utfall_facit': utfall_facit,
        'utfall_wh': utfall_wh,
        'utfall_facit_25': utfall_facit_25,
        'full_year_only_cids': sorted(fyo),
        'unmapped': unmapped,
    }


def run(dash, fyo, facit_dir, mercur, period="202604"):
    """Parsa Mercur-facit + bygg validation. `mercur` = mercur-modulen (injicerad)."""
    from pathlib import Path
    fp = Path(facit_dir) / 'Resultaträkning (20).xlsx'
    facit_2026, facit_2025 = mercur.parse_top_group_facit(fp)
    return build_validation(dash, fyo, facit_2026, facit_2025)
