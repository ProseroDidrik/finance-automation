"""Föreslå kategori + segment för okategoriserade leverantörer.

Algoritm (per land):
  1. Bygg auktoritativ map: supplier_name → vanligaste (kategori, segment)
     från fact_supplier_spend-rader som ÄR kategoriserade.
  2. För varje supplier_name som saknar kategori (eller segment):
     a. Direkt-match (lower-cased) → confidence HIGH
     b. Canonical-match (strip AB/Aktiebolag/AS/Oy/parens, normalisera) → MEDIUM
     c. Första-ord-match (t.ex. 'Iloq Finland AB' → 'Iloq') → LOW
  3. Skriv CSV med förslag, sorterad på total_amount desc.

Output: <base_path>/_statistics/Supplier/_suggested_categories_<country>.csv

Skriver INTE direkt till databasen — användaren granskar CSV:n manuellt och
kan sedan välja att applicera förslagen.

CLI:
    py suggest_categories.py                     # Sweden default
    py suggest_categories.py --country Sweden
    py suggest_categories.py --min-amount 50000  # filtrera bort små
"""
from __future__ import annotations

import argparse
import csv
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

import db
from shared import load_config, log

OUTPUT_SUBDIR = "_statistics/Supplier"

# Suffix som strippas i canonicalize. Ord-gräns krävs.
SUFFIX_RE = re.compile(
    r"\s+(AB|Aktiebolag|A/S|AS|Oy|OY|OYJ|Ojy|GmbH|Ltd|AG|KG|HB|KB|"
    r"S\.A\.|Inc\.?|LLC|ApS|SARL|S\.L\.|N\.V\.|B\.V\.)$",
    re.IGNORECASE,
)
PAREN_RE = re.compile(r"\s*\([^)]*\)\s*")
ASTERISK_PREFIX_RE = re.compile(r"^\*+[^*]*\*+\s*")
WHITESPACE_RE = re.compile(r"\s+")


def canonicalize(name: str | None) -> str:
    """Normalisera ett leverantörsnamn för fuzzy-match.

    'Iloq Finland AB' → 'iloq finland'
    'Bristab Säkerhet AB (Prioritet Finans AB)' → 'bristab säkerhet'
    '***OBS***Höbeco Protection AB' → 'höbeco protection'
    """
    if not name:
        return ""
    s = name.strip()
    s = ASTERISK_PREFIX_RE.sub("", s)
    s = PAREN_RE.sub(" ", s)
    # Strippa suffix iterativt (t.ex. "Foo Bar AB Aktiebolag")
    while True:
        new_s = SUFFIX_RE.sub("", s).strip()
        if new_s == s:
            break
        s = new_s
    s = WHITESPACE_RE.sub(" ", s).strip().lower()
    return s


def first_word(canonical: str) -> str:
    """Första betydande ordet (>=3 tecken). Fallback: hela strängen."""
    parts = canonical.split()
    for p in parts:
        if len(p) >= 3:
            return p
    return canonical


def build_authority_map(
    con,
) -> tuple[dict[str, tuple[str, str | None, int]],
           dict[str, tuple[str, str | None, int]],
           dict[str, tuple[str, str | None, int]]]:
    """Returnera tre uppslagstabeller (exact, canonical, first_word) →
    (kategori, segment, n_supporting_rows). Ranking: namn med flest
    kategoriserade rader vinner."""
    rows = con.execute(
        """SELECT supplier_name, kategori, segment, COUNT(*) AS n
           FROM fact_supplier_spend
           WHERE country = ? AND kategori IS NOT NULL
           GROUP BY supplier_name, kategori, segment""",
        ["Sweden"],
    ).fetchall()

    # För varje supplier_name: pick mest vanliga (kategori, segment)
    by_name: dict[str, Counter] = {}
    for name, kat, seg, n in rows:
        if not name:
            continue
        by_name.setdefault(name, Counter())[(kat, seg)] += n

    name_to_classification: dict[str, tuple[str, str | None, int]] = {}
    for name, ctr in by_name.items():
        (kat, seg), n = ctr.most_common(1)[0]
        name_to_classification[name] = (kat, seg, n)

    exact: dict[str, tuple[str, str | None, int]] = {}
    canonical: dict[str, tuple[str, str | None, int]] = {}
    fword: dict[str, tuple[str, str | None, int]] = {}

    # Aggregera till canonical och first_word (välj högsta-stöd)
    for name, (kat, seg, n) in name_to_classification.items():
        exact_key = name.lower().strip()
        prev = exact.get(exact_key)
        if prev is None or n > prev[2]:
            exact[exact_key] = (kat, seg, n)

        c = canonicalize(name)
        if c:
            prev = canonical.get(c)
            if prev is None or n > prev[2]:
                canonical[c] = (kat, seg, n)

            fw = first_word(c)
            if len(fw) >= 4:  # undvik triviala ord som "och", "ab"
                prev = fword.get(fw)
                if prev is None or n > prev[2]:
                    fword[fw] = (kat, seg, n)

    return exact, canonical, fword


def find_uncategorized(con) -> list[dict]:
    """Returnera per-supplier_name aggregat för Sverige där kategori eller
    segment saknas i NÅGON rad. Sorterat på total amount."""
    rows = con.execute(
        """SELECT supplier_name,
                  SUM(amount)               AS total,
                  COUNT(*)                  AS n_rows,
                  COUNT(*) FILTER (WHERE kategori IS NULL OR segment IS NULL) AS n_missing,
                  COUNT(DISTINCT bolag_label) AS n_bolag
           FROM fact_supplier_spend
           WHERE country = 'Sweden' AND supplier_name IS NOT NULL
           GROUP BY supplier_name
           HAVING COUNT(*) FILTER (WHERE kategori IS NULL OR segment IS NULL) > 0
           ORDER BY total DESC""",
    ).fetchall()
    return [
        {"supplier_name": r[0], "total": float(r[1]) if r[1] else 0.0,
         "n_rows": int(r[2]), "n_missing": int(r[3]), "n_bolag": int(r[4])}
        for r in rows
    ]


def suggest_for(
    name: str,
    exact: dict, canonical: dict, fword: dict,
) -> dict:
    """Returnera {kategori, segment, confidence, via, target, support}."""
    # 1. Exact (case-insensitive)
    key = name.lower().strip()
    if key in exact:
        kat, seg, n = exact[key]
        return {"kategori": kat, "segment": seg,
                "confidence": "HIGH" if n >= 2 else "MEDIUM",
                "via": "exact", "target": key, "support": n}

    # 2. Canonical
    c = canonicalize(name)
    if c and c in canonical:
        kat, seg, n = canonical[c]
        return {"kategori": kat, "segment": seg,
                "confidence": "HIGH" if n >= 3 else "MEDIUM" if n >= 2 else "LOW",
                "via": "canonical", "target": c, "support": n}

    # 3. First word
    if c:
        fw = first_word(c)
        if len(fw) >= 4 and fw in fword:
            kat, seg, n = fword[fw]
            return {"kategori": kat, "segment": seg,
                    "confidence": "MEDIUM" if n >= 3 else "LOW",
                    "via": "first_word", "target": fw, "support": n}

    return {"kategori": None, "segment": None, "confidence": "NONE",
            "via": None, "target": None, "support": 0}


def write_csv(out_path: Path, rows: list[dict]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "supplier_name", "total_amount", "n_rows", "n_missing", "n_bolag",
            "suggested_kategori", "suggested_segment",
            "confidence", "match_via", "match_target", "support_rows",
        ])
        w.writeheader()
        for r in rows:
            w.writerow(r)


def run(country: str, min_amount: float) -> int:
    config = load_config()
    base_path = Path(config["base_path"])
    period = datetime.now().strftime("%Y%m")
    log("START", "suggest_categories.py", f"country={country} period={period}")

    if country != "Sweden":
        log("ERROR", country, "endast Sweden stöds just nu")
        return 1

    con = db.connect(read_only=True)
    try:
        exact, canonical, fword = build_authority_map(con)
        log("INFO", country,
            f"auth-map: exact={len(exact)} canonical={len(canonical)} first_word={len(fword)}")

        uncats = find_uncategorized(con)
        log("INFO", country, f"okategoriserade leverantörer: {len(uncats)}")

        out_rows = []
        n_high = n_med = n_low = n_none = 0
        sum_suggested = 0.0
        for u in uncats:
            if u["total"] < min_amount:
                continue
            sug = suggest_for(u["supplier_name"], exact, canonical, fword)
            out_rows.append({
                "supplier_name":     u["supplier_name"],
                "total_amount":      round(u["total"], 2),
                "n_rows":            u["n_rows"],
                "n_missing":         u["n_missing"],
                "n_bolag":           u["n_bolag"],
                "suggested_kategori": sug["kategori"] or "",
                "suggested_segment": sug["segment"] or "",
                "confidence":        sug["confidence"],
                "match_via":         sug["via"] or "",
                "match_target":      sug["target"] or "",
                "support_rows":      sug["support"],
            })
            if sug["confidence"] == "HIGH":   n_high += 1
            elif sug["confidence"] == "MEDIUM": n_med += 1
            elif sug["confidence"] == "LOW":  n_low += 1
            else: n_none += 1
            if sug["kategori"]:
                sum_suggested += u["total"]

        out_path = base_path / OUTPUT_SUBDIR / f"_suggested_categories_{country}.csv"
        write_csv(out_path, out_rows)
        rel = db.relpath_from_base(out_path, base_path)
        log("OK", country,
            f"skrev {len(out_rows)} förslag till {rel}")
        log("INFO", country,
            f"HIGH={n_high}  MEDIUM={n_med}  LOW={n_low}  NONE={n_none}  "
            f"summa förslag-täckt={sum_suggested:,.0f}")
        log("DONE", "suggest_categories.py", "klar")
        return 0
    finally:
        con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--country", default="Sweden", help="just nu bara 'Sweden'")
    ap.add_argument("--min-amount", type=float, default=0.0,
                    help="filtrera bort leverantörer under detta belopp")
    args = ap.parse_args()
    raise SystemExit(run(args.country, args.min_amount))


if __name__ == "__main__":
    main()
