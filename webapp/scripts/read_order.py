"""Läs _uploads/Ordning på rader.xlsx"""
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[2]
fp = REPO / "_uploads" / "Ordning på rader.xlsx"

# Försök först pandas (xlrd/openpyxl), sedan fallback via xlsx2csv eller python-calamine
try:
    df = pd.read_excel(fp, sheet_name=None, engine="openpyxl")
except Exception as e:
    print(f"openpyxl failed: {e}")
    print("trying calamine...")
    df = pd.read_excel(fp, sheet_name=None, engine="calamine")

for name, sheet in df.items():
    print(f"=== Sheet: {name} ({sheet.shape}) ===")
    print(sheet.to_string())
    print()
