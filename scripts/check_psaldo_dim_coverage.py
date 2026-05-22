"""Spot-check för Bug 2 (#PSALDO dim-dedup): hittar SIE-filer där load_sie.py:s
{}-only-regel skulle tappa #PSALDO-konton som saknar en {}-totalrad.

Kör INNAN SIE-omladdning efter Bug 2-fixen:
    py scripts/check_psaldo_dim_coverage.py                  # extracted/{prev}/Sweden
    py scripts/check_psaldo_dim_coverage.py --period 202604
    py scripts/check_psaldo_dim_coverage.py --source-dir <mapp>
    py scripts/check_psaldo_dim_coverage.py <fil1.SE> <fil2.SE> ...

Exit 0 = GRÖNT (ingen fil tappar #PSALDO-data -> kör omladdningen).
Exit 1 = RÖTT (minst en fil har konton som saknar {}-total).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import load_sie
from shared import load_config, prev_month_period


def discover(source_dir: Path) -> list[Path]:
    """SIE-filer direkt i source_dir (samma suffix som load_sie.discover_files)."""
    if not source_dir.exists():
        return []
    return sorted(f for f in source_dir.iterdir()
                  if f.is_file() and f.suffix.upper() in {".SE", ".SI", ".SIE"})


def check_file(path: Path) -> bool:
    """Kontrollera en fil. Returnerar True om OK (inga konton skulle tappas)."""
    try:
        text = load_sie.read_text_with_fallback(path)
    except Exception as e:
        print(f"[ERROR]   {path.name}: läsfel: {e}")
        return False

    cov = load_sie.psaldo_dim_coverage(text)
    lost = cov["lost_accounts"]

    if not cov["total_row_count"] and not lost:
        print(f"[INFO]    {path.name}: inga #PSALDO-rader")
        return True
    if not lost:
        print(f"[OK]      {path.name}: {cov['total_row_count']} {{}}-totalrader, "
              f"{cov['all_psaldo_accounts']} konton — alla har {{}}-total")
        return True

    preview = ", ".join(lost[:12]) + (" ..." if len(lost) > 12 else "")
    print(f"[FÖRLUST] {path.name}: {len(lost)} konton saknar {{}}-total och "
          f"skulle tappas av fixen: {preview}")
    return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="*",
                        help="SIE-filer att kontrollera direkt (annars skannas en mapp)")
    parser.add_argument("--period", default=None,
                        help="YYYYMM (default: föregående månad)")
    parser.add_argument("--source-dir", default=None,
                        help="Mapp att skanna (default: extracted/{period}/Sweden)")
    args = parser.parse_args()

    if args.files:
        files = [Path(f) for f in args.files]
    else:
        period = args.period or prev_month_period()
        if args.source_dir:
            source_dir = Path(args.source_dir)
        else:
            source_dir = Path(load_config()["base_path"]) / "extracted" / period / "Sweden"
        print(f"[START]   Skannar {source_dir}")
        files = discover(source_dir)

    if not files:
        print("[DONE]    Inga SIE-filer hittades")
        return

    ok = sum(check_file(f) for f in files)
    fail = len(files) - ok
    print(f"[DONE]    {ok} OK  {fail} med fynd  ({len(files)} filer)")
    if fail:
        print("          -> RÖTT: rapportera fynden innan omladdning.")
        sys.exit(1)
    print("          -> GRÖNT: fixen tappar ingen #PSALDO-data. Kör omladdningen.")


if __name__ == "__main__":
    main()
