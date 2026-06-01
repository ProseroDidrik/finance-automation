"""Gör paketmappen importerbar för testerna (flata importer: aggregate, validate…)."""
import sys
from pathlib import Path

_PKG = Path(__file__).resolve().parents[1]
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))
