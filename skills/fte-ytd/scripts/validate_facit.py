"""Validera warehouse-data mot Mercur Resultaträkning-facit.

Använder mappnings-tabellen i references/mercur_mapping.md och bygger validation_final.json
med diff per RU per top_group.
"""
import re
from difflib import SequenceMatcher


# Manuell mappning (hämtad från references/mercur_mapping.md)
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
}


def norm(s):
    s = s.lower()
    s = re.sub(r'\b(ab|as|a/s|oy|gmbh|aps|konsoliderad|konsoliderat|konsolidiert|aktiebolag|sicherheitssysteme|sicherheitstechnik|inkl|group)\b', ' ', s)
    s = re.sub(r'[^\w\s]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def map_mercur_to_ru(facit_b, ru_meta, cid_to_co):
    """Returnera dict {mercur_name: ru_dict, ...} + lista över unmapped."""
    cid_to_ru = {ru['reporting_cid']: ru for ru in ru_meta.values()}
    ru_by_norm = {}
    for r in ru_meta.values():
        ru_by_norm.setdefault(norm(r['name']), []).append(r)

    mapping = {}
    unmapped = []
    for mname in facit_b:
        if mname == 'Utfall':
            continue
        if mname in MERCUR_TO_CID:
            cid = MERCUR_TO_CID[mname]
            if cid is None:
                unmapped.append({'name': mname, 'reason': 'manual_skip'})
                continue
            if cid in cid_to_ru:
                mapping[mname] = cid_to_ru[cid]
                continue
            c = cid_to_co.get(cid)
            if c and c.get('parent_id') and c['parent_id'] in cid_to_ru:
                mapping[mname] = cid_to_ru[c['parent_id']]
                continue
            unmapped.append({'name': mname, 'reason': f'cid_{cid}_no_ru'})
            continue

        # Try normalized exact match
        mn = norm(mname)
        if mn in ru_by_norm:
            for kind in ['standalone', 'consolidated', 'orphan_sub']:
                for ru in ru_by_norm[mn]:
                    if ru['kind'] == kind:
                        mapping[mname] = ru
                        break
                if mname in mapping:
                    break
            if mname in mapping:
                continue

        # Token-overlap fuzzy
        mn_toks = set(mn.split())
        best = None
        bs = 0
        for ru in ru_meta.values():
            rn = norm(ru['name'])
            rn_toks = set(rn.split())
            if not rn_toks:
                continue
            overlap = len(rn_toks & mn_toks) / min(len(rn_toks), len(mn_toks))
            if (rn in mn or mn in rn) and overlap >= 0.5:
                s = min(len(rn), len(mn)) / max(len(rn), len(mn))
                if s > bs:
                    best = ru
                    bs = s
            elif overlap >= 1.0:
                if overlap > bs:
                    best = ru
                    bs = overlap
        if best and bs >= 0.5:
            mapping[mname] = best
        else:
            unmapped.append({'name': mname, 'reason': 'no_match'})

    return mapping, unmapped


TOP_GROUPS = ['Total Sales', 'Total Direct Cost', 'Bruttovinst', 'Personnel',
              'Consultants', 'Other External Costs', 'Premises', 'Transportation',
              'Depreciation', 'Justerad EBITDA']

TG_KPI = {'Total Sales': 'sales', 'Total Direct Cost': 'tdc', 'Personnel': 'personnel',
          'Consultants': 'consultants', 'Other External Costs': 'other_ext',
          'Premises': 'premises', 'Transportation': 'transport',
          'Depreciation': 'depreciation', 'Bruttovinst': 'gross_profit',
          'Justerad EBITDA': 'ebitda'}


def build_validation(facit_b, mapping, wh_by_cid):
    """Bygg validation rows och utfall/RU-total."""
    validation = []
    for mname, ru in mapping.items():
        fb = facit_b[mname]
        wh = wh_by_cid.get(ru['reporting_cid'], {})
        k26 = (wh.get('kpis') or {}).get('202604') or {}
        row = {
            'mercur_name': mname, 'reporting_cid': ru['reporting_cid'],
            'name': ru['name'], 'country': ru['country'], 'kind': ru['kind'],
            'member_cids': ru['member_cids'],
            'cids_with_data': [ru['reporting_cid']] if k26.get('sales') else [],
        }
        for tg in TOP_GROUPS:
            fv = abs(fb.get(tg, 0))
            wv = abs(k26.get(TG_KPI[tg], 0) or 0)
            d = fv - wv
            dp = d / fv if fv > 1000 else None
            row[tg] = {'facit': round(fv), 'wh': round(wv), 'diff': round(d), 'diff_pct': dp}
        validation.append(row)
    return validation
