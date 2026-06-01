"""render_html.py — generera dashboarden ur en template (ej fragil patchning).

Cowork v13:s update_html.py PATCHADE en befintlig HTML genom att hitta JSON-blob-
gränser med raw_decode och klippa runt dem. Det är skört. Här i stället:

  * extract_template() körs EN gång (build-time, eller om template behöver byggas
    om): läser v13-HTML:en, lokaliserar de tre `const X = {...};`-blobarna med
    samma raw_decode-teknik, och ersätter varje JSON-VÄRDE med en token. Resultatet
    committas som templates/dashboard_base.html (CSS+JS+skelett, utan data).
  * render() injicerar färsk data via ren token-substitution. Ingen blob-gräns-
    detektion vid varje körning — bara str.replace.

JS:en i templaten läser DATA/VALIDATION/AARO_DATA. Aaro är uppskjuten → vi injicerar
`[]` för AARO_DATA så att fliken inte kraschar (node --check fångar inte runtime).
"""
from __future__ import annotations

import json
from pathlib import Path

TOKENS = {'DATA': '__DATA_JSON__', 'VALIDATION': '__VALIDATION_JSON__',
          'AARO_DATA': '__AARO_JSON__'}


def _find_blob(content: str, label: str):
    """(blob_start, blob_end) för JSON-värdet efter `const {label} = ` (exkl. ';')."""
    marker = f'const {label} = '
    start = content.find(marker)
    if start < 0:
        raise ValueError(f'Hittade inte `const {label} = ` i HTML:en')
    blob_start = start + len(marker)
    _, end_off = json.JSONDecoder().raw_decode(content[blob_start:])
    return blob_start, blob_start + end_off


def extract_template(src_html: Path, out_template: Path) -> Path:
    """Bygg dashboard_base.html ur en färdig v13-HTML genom att tokenisera blobarna."""
    content = Path(src_html).read_text(encoding='utf-8')
    # Ersätt bakifrån så tidigare offset inte förskjuts.
    spans = sorted((_find_blob(content, lbl) + (lbl,) for lbl in TOKENS),
                   key=lambda s: s[0], reverse=True)
    for blob_start, blob_end, lbl in spans:
        content = content[:blob_start] + TOKENS[lbl] + content[blob_end:]
    Path(out_template).write_text(content, encoding='utf-8')
    return out_template


def render(dash: dict, validation, template_path: Path, out_path: Path) -> Path:
    """Injicera dash/validation/aaro i templaten och skriv klar HTML."""
    tmpl = Path(template_path).read_text(encoding='utf-8')
    payloads = {
        'DATA': dash,
        'VALIDATION': validation or {'rows': [], 'utfall_facit': {}, 'utfall_wh': {},
                                     'utfall_facit_25': {}, 'full_year_only_cids': []},
        'AARO_DATA': [],   # uppskjuten — tom array håller JS:en glad
    }
    for lbl, tok in TOKENS.items():
        blob = json.dumps(payloads[lbl], ensure_ascii=False, default=str)
        if tok not in tmpl:
            raise ValueError(f'Token {tok} saknas i templaten — bygg om med extract_template()')
        tmpl = tmpl.replace(tok, blob)
    Path(out_path).write_text(tmpl, encoding='utf-8')
    return out_path
