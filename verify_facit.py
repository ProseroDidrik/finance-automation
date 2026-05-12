"""Jämför facit-INL.xlsx mot genererade INL-filer för en period.

Facit ligger i {base_path}/_inbox/Facit/{period}/*.xlsx (1 fil per bolag).
Genererad INL ligger i {base_path}/extracted/{period}/{Country}/output/
{ID:03d}_*_INL.xlsx (eller motsvarande efter process_*.py).

Filnamn-konventioner skiljer sig per period:
  202602:  '{ID:03d}_{name}_{period}_INL.xlsx'           (lätt — prefix)
  202603:  '{name} 202603 INL.xlsx' / '{name} mar Inl.xlsx'  (svår — namn)
  202604:  liknande blandning som 202603

Resolution-strategi för bolag-id ur facit-filnamn:
  1. Explicit override i _params/facit_overrides.json (om filen finns)
  2. ^\\d{3}[_\\s]-prefix → direkt id
  3. Token-overlap mot dim_company.name + aliases ur _params/overrides.json
  4. UNKNOWN → flaggas i rapporten, jämförelse hoppas över

Jämförelse: läs båda INL-filerna via load_inl.read_inl_rows(), bygg
{account_code: amount}-dict, rapportera:
  - antal konton diff (saknade/extra i någondera filen)
  - per-konto avvikelser (abs > --tolerance, default 1.0)
  - sum-diff
Rapport per bolag: OK / DIFF (med första 5 detaljer).

CLI:
    py verify_facit.py --period 202604
    py verify_facit.py --period 202603 --tolerance 0.5
    py verify_facit.py --period 202602 --company 229
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from load_inl import read_inl_rows
from shared import load_config, load_dotterbolag_full, load_overrides

REPO_ROOT = Path(__file__).resolve().parent
DOTTERBOLAG_PATH = REPO_ROOT / "_params" / "Dotterbolagslista.xlsx"
FACIT_OVERRIDES_PATH = REPO_ROOT / "_params" / "facit_overrides.json"

PREFIX_RE = re.compile(r"^(\d{3})[_\s]")
TOKEN_RE = re.compile(r"[A-Za-zÅÄÖåäöÆØæø]{3,}")
STOPWORDS = {
    "inl", "ny", "mar", "mars", "apr", "april", "feb", "februari",
    "jan", "januari", "ab", "oy", "as", "gmbh", "aps", "ltd", "fil",
    "nettad", "fram", "202602", "202603", "202604", "202601",
    "huhtikuu", "tom", "and", "for", "from",
}


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in TOKEN_RE.findall(text) if t.lower() not in STOPWORDS}


def _load_facit_overrides() -> dict[str, int]:
    if not FACIT_OVERRIDES_PATH.exists():
        return {}
    try:
        with open(FACIT_OVERRIDES_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, int] = {}
    for k, v in data.items():
        if isinstance(k, str) and k.startswith("_"):
            continue
        try:
            out[str(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def _build_company_index(dotterbolag: dict[int, dict], overrides: dict) -> dict[int, set[str]]:
    """company_id → set of normalised tokens from name + aliases."""
    alias_map: dict[int, list[str]] = {}
    for k, phrases in (overrides.get("aliases") or {}).items():
        try:
            cid = int(k)
        except (TypeError, ValueError):
            continue
        alias_map[cid] = [p for p in phrases if p]

    out: dict[int, set[str]] = {}
    for cid, meta in dotterbolag.items():
        if (meta.get("kind") or "").lower() == "consolidated":
            continue
        toks = _tokens(meta.get("name") or "")
        for alias in alias_map.get(cid, []):
            toks |= _tokens(alias)
        if toks:
            out[cid] = toks
    return out


def resolve_bolag_id(stem: str, company_tokens: dict[int, set[str]],
                     facit_overrides: dict[str, int]) -> tuple[int | None, str]:
    """Returnerar (bolag_id, källa) eller (None, 'unknown')."""
    if stem in facit_overrides:
        return facit_overrides[stem], "override"

    m = PREFIX_RE.match(stem)
    if m:
        return int(m.group(1)), "prefix"

    file_toks = _tokens(stem)
    if not file_toks:
        return None, "unknown"

    scores: list[tuple[int, int, set[str]]] = []
    for cid, name_toks in company_tokens.items():
        overlap = file_toks & name_toks
        if overlap:
            scores.append((cid, len(overlap), overlap))
    if not scores:
        return None, "unknown"

    scores.sort(key=lambda x: x[1], reverse=True)
    best_score = scores[0][1]
    leaders = [s for s in scores if s[1] == best_score]
    if len(leaders) == 1:
        return leaders[0][0], f"token(score={best_score})"
    return None, f"ambiguous({','.join(str(s[0]) for s in leaders[:3])})"


def _find_output_inl(extracted_root: Path, period: str, country: str, bolag_id: int) -> Path | None:
    """output/{ID:03d}_*_INL.xlsx i extracted/{period}/{country}/output/."""
    out_dir = extracted_root / period / country / "output"
    if not out_dir.exists():
        return None
    prefix = f"{bolag_id:03d}_"
    candidates = sorted(p for p in out_dir.glob(f"{prefix}*_INL.xlsx") if p.is_file())
    return candidates[-1] if candidates else None


def _read_inl_amounts(path: Path) -> dict[str, float]:
    """account_code → amount. Vid duplicates summeras (matchar load_inl-semantiken)."""
    rows, _ = read_inl_rows(path)
    out: dict[str, float] = {}
    for r in rows:
        acc = str(r[0]).strip()
        amt = float(r[2])
        out[acc] = out.get(acc, 0.0) + amt
    return out


def compare(facit_path: Path, our_path: Path, tolerance: float
            ) -> tuple[bool, list[str]]:
    """True om allt OK, annars (False, [diffar])."""
    f = _read_inl_amounts(facit_path)
    o = _read_inl_amounts(our_path)
    issues: list[str] = []

    only_facit = sorted(set(f) - set(o))
    only_ours = sorted(set(o) - set(f))
    if only_facit:
        issues.append(f"endast i facit: {len(only_facit)} konton ({', '.join(only_facit[:5])}"
                      + ("..." if len(only_facit) > 5 else "") + ")")
    if only_ours:
        issues.append(f"endast i vår: {len(only_ours)} konton ({', '.join(only_ours[:5])}"
                      + ("..." if len(only_ours) > 5 else "") + ")")

    mismatches: list[tuple[str, float, float]] = []
    for acc in sorted(set(f) & set(o)):
        if abs(f[acc] - o[acc]) > tolerance:
            mismatches.append((acc, f[acc], o[acc]))
    if mismatches:
        head = ", ".join(f"{a}={fv:.2f}->{ov:.2f}" for a, fv, ov in mismatches[:5])
        more = f" (+{len(mismatches) - 5} till)" if len(mismatches) > 5 else ""
        issues.append(f"avvikande belopp: {len(mismatches)} konton ({head}{more})")

    sum_f = sum(f.values())
    sum_o = sum(o.values())
    if abs(sum_f - sum_o) > tolerance:
        issues.append(f"sum-diff: facit={sum_f:.2f}  vår={sum_o:.2f}  diff={sum_f-sum_o:.2f}")

    return not issues, issues


def run(period: str, only_company: int | None, tolerance: float) -> int:
    cfg = load_config()
    base = Path(cfg["base_path"])
    facit_dir = base / "_inbox" / "Facit" / period
    extracted_root = base / "extracted"

    if not facit_dir.exists():
        print(f"FEL: facit-mapp saknas: {facit_dir}")
        return 2

    dotterbolag = load_dotterbolag_full(DOTTERBOLAG_PATH)
    overrides = load_overrides()
    facit_overrides = _load_facit_overrides()
    company_tokens = _build_company_index(dotterbolag, overrides)

    files = sorted(facit_dir.glob("*.xlsx"))
    if not files:
        print(f"FEL: inga .xlsx i {facit_dir}")
        return 2

    print(f"=== verify_facit  period={period}  tolerance={tolerance}  ({len(files)} facit-filer) ===\n")
    ok = mismatch = missing = unknown = 0
    unknown_files: list[str] = []

    for facit_path in files:
        stem = facit_path.stem
        bolag_id, source = resolve_bolag_id(stem, company_tokens, facit_overrides)

        if bolag_id is None:
            unknown += 1
            unknown_files.append(stem)
            print(f"  [UNKNOWN]  {stem}    ({source})")
            continue
        if only_company is not None and bolag_id != only_company:
            continue
        if bolag_id not in dotterbolag:
            print(f"  [UNKNOWN]  {stem}    (id {bolag_id} ej i Dotterbolagslistan)")
            unknown += 1
            continue

        meta = dotterbolag[bolag_id]
        country = meta.get("country") or "Other"
        our_path = _find_output_inl(extracted_root, period, country, bolag_id)
        if our_path is None:
            missing += 1
            print(f"  [MISSING]  {bolag_id:3d}  {meta.get('name','?'):30s}  "
                  f"({source})  ingen output-INL i extracted/{period}/{country}/output/")
            continue

        try:
            all_ok, issues = compare(facit_path, our_path, tolerance)
        except Exception as e:
            mismatch += 1
            print(f"  [ERROR]    {bolag_id:3d}  {meta.get('name','?'):30s}  {e}")
            continue

        if all_ok:
            ok += 1
            print(f"  [OK]       {bolag_id:3d}  {meta.get('name','?'):30s}  ({source})  "
                  f"vs {our_path.name}")
        else:
            mismatch += 1
            print(f"  [DIFF]     {bolag_id:3d}  {meta.get('name','?'):30s}  ({source})  "
                  f"vs {our_path.name}")
            for issue in issues:
                print(f"             {issue}")

    print(f"\n=== summa: {ok} OK  {mismatch} DIFF  {missing} MISSING  {unknown} UNKNOWN ===")
    if unknown_files:
        print(f"\nUnknown-filer kan adderas till _params/facit_overrides.json (mapp stem→bolag_id):")
        for u in unknown_files:
            print(f'    "{u}": <ID>,')
    return 0 if (mismatch == 0 and missing == 0 and unknown == 0) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--period", required=True, help="YYYYMM")
    parser.add_argument("--company", type=int, default=None, help="Bara ett bolag")
    parser.add_argument("--tolerance", type=float, default=1.0,
                        help="Tröskel per konto + sum-diff (default: 1.0)")
    args = parser.parse_args()
    sys.exit(run(args.period, args.company, args.tolerance))


if __name__ == "__main__":
    main()
