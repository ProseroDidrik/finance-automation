"""Parsa Mercur Resultaträkning-exporter (.xlsx).

Tre filer:
- Resultaträkning Bolag.xlsx: bolagslista med indenterade rader
- Resultaträkning (20).xlsx: top_group-nivå per bolag (Total Försäljning etc.)
- Resultaträkning (21).xlsx: aaro-konto-nivå per bolag

Använder python_calamine för att läsa (openpyxl kan failera på Mercurs filer).
"""
import re
import python_calamine


def parse_bolag(fp):
    """Parsa Resultaträkning Bolag.xlsx → lista över bolag med (cid, name, indent, parent_cid).

    Indentering visar hierarki:
    - indent 2 = top level (standalone eller consolidated)
    - indent 4 = sub under consolidated (parent = previous top-level)
    """
    wb = python_calamine.CalamineWorkbook.from_path(fp)
    ws = wb.get_sheet_by_name('Resultaträkning')
    rows = ws.to_python()

    bolag = []
    last_top = None
    for ri, row in enumerate(rows[10:], start=11):  # skip meta + header
        label = row[0] if row[0] else ''
        if not isinstance(label, str) or not label.strip():
            continue
        stripped = label.lstrip()
        indent = len(label) - len(stripped)
        m = re.match(r'^(\d+)\s+(.+)$', stripped)
        if not m:
            continue
        cid = int(m.group(1))
        name = m.group(2).strip()
        is_top = indent == 2
        is_sub = indent >= 4

        if is_top:
            last_top = {'cid': cid, 'name': name}

        bolag.append({
            'row': ri, 'indent': indent, 'cid': cid, 'name': name,
            'is_top_level': is_top, 'is_sub': is_sub,
            'parent_cid': last_top['cid'] if is_sub and last_top else None,
        })

    # Identify which top-level are TRULY consolidated (have ≥1 sub child following)
    cons_cids = set()
    for i, b in enumerate(bolag):
        if b['is_top_level']:
            for j in range(i + 1, len(bolag)):
                if bolag[j]['is_top_level']:
                    break
                if bolag[j]['is_sub']:
                    cons_cids.add(b['cid'])
                    break

    for b in bolag:
        if b['cid'] in cons_cids:
            b['type'] = 'consolidated'
        elif b['is_sub']:
            b['type'] = 'sub'
        else:
            b['type'] = 'standalone'

    return bolag


ROW_TO_TG = {
    'Total Försäljning': 'Total Sales',
    'Summa direkta kostnader': 'Total Direct Cost',
    'Lokalkostnader': 'Premises',
    'Transportkostnader': 'Transportation',
    'Konsultkostnader': 'Consultants',
    'Övriga externa kostnader': 'Other External Costs',
    'Personalkostnader': 'Personnel',
    'Bruttovinst': 'Bruttovinst',
    'Justerad EBITDA': 'Justerad EBITDA',
    'Avskrivningar (fixed assets)': 'Depreciation_fixed',
    'Avskrivningar (leased assets)': 'Depreciation_leased',
}


def _unique_cols(hdr, lo, hi):
    """Unika (col_idx, header) i kolumnintervallet [lo, hi), dedupat på namn.

    KRITISKT: bolagsnamnen UPPREPAS mellan 2026- och 2025-regionen. Att deduppa
    per region (inte globalt) är hela poängen — annars fångas bara 2026 (vilket
    var buggen i v1.6: split_col ignorerades helt och 2025 föll bort).
    """
    seen, out = set(), []
    for i in range(lo, min(hi, len(hdr))):
        h = hdr[i]
        if not h:
            continue
        h = h.strip() if isinstance(h, str) else str(h)
        if h in seen:
            continue
        seen.add(h)
        out.append((i, h))
    return out


def _build_facit(rows, cols):
    """Bygg {bolag_name: {top_group: amount}} för en uppsättning kolumner."""
    facit = {}
    for row in rows[8:]:
        if not row[0]:
            continue
        label = row[0].strip() if isinstance(row[0], str) else None
        if label not in ROW_TO_TG:
            continue
        tg = ROW_TO_TG[label]
        for col_idx, bn in cols:
            if col_idx >= len(row):
                continue
            v = row[col_idx]
            if v in (None, ''):
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            facit.setdefault(bn, {})
            if tg.startswith('Depreciation'):
                facit[bn]['Depreciation'] = facit[bn].get('Depreciation', 0) + v
            else:
                facit[bn][tg] = v
    return facit


def parse_top_group_facit(fp, split_col=111):
    """Parsa Resultaträkning (20).xlsx → (facit_2026, facit_2025).

    Mercur har TVÅ kolumnregioner i SAMMA fil (samma bolagsnamn upprepas):
    - Col 1      = 'Utfall'        → 2026 koncerntotal
    - Col 2..110 = 2026 utfall per bolag
    - Col 111    = 'Utfall fg. år' → 2025 koncerntotal
    - Col 112+   = 2025 utfall per bolag

    Returnerar (facit_2026, facit_2025), båda {bolag_name: {top_group: amount}}.
    Koncerntotalen ligger under key 'Utfall' i BÅDA dicts ('Utfall fg. år'
    normaliseras till 'Utfall') så nedströms-koden slår upp likadant.
    """
    wb = python_calamine.CalamineWorkbook.from_path(fp)
    ws = wb.get_sheet_by_name('Resultaträkning')
    rows = ws.to_python()
    hdr = rows[7]

    facit_2026 = _build_facit(rows, _unique_cols(hdr, 0, split_col))
    facit_2025 = _build_facit(rows, _unique_cols(hdr, split_col, len(hdr)))

    if 'Utfall fg. år' in facit_2025:
        facit_2025['Utfall'] = facit_2025.pop('Utfall fg. år')

    return facit_2026, facit_2025


def _parse_aaro_label(label):
    """'  Sales 3010 Sales, external' → ('Sales', '3010', 'Sales, external').

    account_id kan innehålla mellanslag ('Other Sales') — den icke-giriga (.+?)
    expanderar tills den 4-siffriga aaro-koden matchar.
    """
    m = re.search(r'^(.+?)\s+(\d{4})\s+(.+)$', label.strip())
    if not m:
        return None
    return m.group(1).strip(), m.group(2), m.group(3).strip()


def parse_aaro_facit(fp):
    """Parsa Resultaträkning (21).xlsx → (aaro_2026, aaro_2025).

    Filen listar per AARO-konto (indent 2: '{account_id} {aaro_code:4} {desc}')
    under en indent-0 top_group ('Försäljning', 'Materialkostnader', ...). Vi tar
    KONCERN-utfallet per rad: kolumnen 'Utfall' (2026) resp 'Utfall fg. år' (2025)
    — samma två-regions-split som parse_top_group_facit (Col 1 vs Col 111).

    Returnerar (aaro_2026, aaro_2025), båda list[{top_group, account_id, aaro_code,
    desc, utfall}]. utfall i rå SEK (Mercur-konvention). Rad-ordningen bevaras och
    är identisk mellan åren (samma AARO-rader).
    """
    wb = python_calamine.CalamineWorkbook.from_path(fp)
    ws = wb.get_sheet_by_name('Resultaträkning')
    rows = ws.to_python()
    hdr = rows[8]

    def _koncern_col(target):
        for i, h in enumerate(hdr):
            if isinstance(h, str) and h.strip() == target:
                return i
        raise ValueError(f"Hittade inte koncernkolumn {target!r} i Resultaträkning (21)")

    col_2026 = _koncern_col('Utfall')          # Col 1
    col_2025 = _koncern_col('Utfall fg. år')   # Col 111

    def _val(row, ci):
        v = row[ci] if ci < len(row) else None
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    out_2026, out_2025 = [], []
    current_top = None
    for row in rows[9:]:
        if not row or not row[0] or not isinstance(row[0], str) or not row[0].strip():
            continue
        label = row[0]
        indent = len(label) - len(label.lstrip())
        if indent == 0:
            current_top = label.strip()
            continue
        p = _parse_aaro_label(label)
        if not p:
            continue
        account_id, aaro_code, desc = p
        base = {'top_group': current_top, 'account_id': account_id,
                'aaro_code': aaro_code, 'desc': desc}
        out_2026.append({**base, 'utfall': _val(row, col_2026)})
        out_2025.append({**base, 'utfall': _val(row, col_2025)})
    return out_2026, out_2025
          