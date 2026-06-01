"""fx.py — månadsvisa valutakurser (genomsnittskurs mot SEK) ur _params/Valutakurser.xlsx.

Ersätter config.FX:s hårdkodade enkurs-per-jämförelseperiod. Dashboarden konverterar
NU per månad: varje månads rörelse × den månadens snittkurs (så som Mercur räknar) —
en enda YTD-kurs gav ~1,4 % FX-fel på NOK-bolag (se known_pitfalls / FX-utredning).

Källa: `_params/Valutakurser.xlsx`, flik **'Genomsnittskurs'**:
  rad 2 = månadsrubriker ('Dec 2019', 'Jan 2020', ... svensk månadsförkortning + år),
  rad 3+ = en rad per valuta ('NOK'/'DKK'/'EUR') med kursen (SEK per 1 enhet utländsk).

`dim_exchange_rate` i warehouse saknar idag 202604 NOK — därför läser vi den
auktoritativa, kompletta xlsx-filen direkt (samma fil som borde mata tabellen).
Seam: byt `load_monthly_rates` mot en DB-hämtning när tabellen är ifylld.
"""
from __future__ import annotations

from pathlib import Path

# Svenska månadsförkortningar i Valutakurser.xlsx → månadsnummer.
_SV_MONTH = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "maj": "05", "jun": "06",
    "jul": "07", "aug": "08", "sep": "09", "okt": "10", "nov": "11", "dec": "12",
}

# Fallback om en period/valuta saknas i filen (1:1 SEK + närmaste rimliga kurs).
FX_FALLBACK = {"SEK": 1.0, "NOK": 0.95, "DKK": 1.48, "EUR": 11.0}


def _header_to_period(text) -> str | None:
    """'Apr 2026' → '202604'. Returnerar None om rubriken inte är 'Mån ÅÅÅÅ'."""
    if not text:
        return None
    parts = str(text).strip().split()
    if len(parts) != 2:
        return None
    mon = _SV_MONTH.get(parts[0][:3].lower())
    if not mon or not parts[1].isdigit():
        return None
    return f"{parts[1]}{mon}"


def load_monthly_rates(path: str | Path) -> dict[str, dict[str, float]]:
    """Läs 'Genomsnittskurs'-fliken → {period(YYYYMM): {currency: rate, 'SEK': 1.0}}.

    Endast perioder/valutor med ett numeriskt värde tas med. SEK=1.0 läggs alltid
    till per period. Raises FileNotFoundError om filen saknas (medvetet — vi vill
    INTE tyst falla tillbaka på fel kurs).
    """
    import openpyxl

    wb = openpyxl.load_workbook(Path(path), data_only=True, read_only=True)
    try:
        ws = wb["Genomsnittskurs"]
        rows = list(ws.iter_rows(values_only=True))
    finally:
        wb.close()

    header = rows[1]  # rad 2 (0-indexerat 1)
    col_period = {ci: _header_to_period(v) for ci, v in enumerate(header)}

    out: dict[str, dict[str, float]] = {}
    for row in rows[2:]:
        cur = (row[0] or "").strip() if row and row[0] else ""
        if cur not in ("NOK", "DKK", "EUR"):
            continue
        for ci, val in enumerate(row):
            per = col_period.get(ci)
            if per is None or val is None:
                continue
            out.setdefault(per, {"SEK": 1.0})[cur] = float(val)
    return out


def rate(monthly_rates: dict[str, dict[str, float]], period: str, currency: str) -> float:
    """Kurs för (period, valuta) med fallback. SEK alltid 1.0."""
    if currency == "SEK":
        return 1.0
    row = monthly_rates.get(period)
    if row and currency in row:
        return row[currency]
    return FX_FALLBACK.get(currency, 1.0)


def default_fx_path(repo_root: Path) -> Path:
    """Hitta Valutakurser.xlsx: worktree-_params först, annars main-repots _params.

    Worktrees (`<main>/.claude/worktrees/<namn>`) har ett _params-skelett men saknar
    den gitignore:ade xlsx-filen — falla då tillbaka på huvud-repots _params.
    """
    cand = repo_root / "_params" / "Valutakurser.xlsx"
    if cand.exists():
        return cand
    parts = repo_root.parts
    if ".claude" in parts:
        main_root = Path(*parts[: parts.index(".claude")])
        alt = main_root / "_params" / "Valutakurser.xlsx"
        if alt.exists():
            return alt
    return cand  # låt anroparen få FileNotFoundError mot den förväntade sökvägen
