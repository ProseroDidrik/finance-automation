"""Punkt 2: torrkör load_file på bolag 104:s 2022 SAF-T-fil för att se om den
laddar rent (orgnr/period/XSD/balans). read_only-anslutning + dry_run=True →
GARANTERAT inga skrivningar. Avgör om gapet ska fyllas (ladda) eller om den
föräldralösa analysen ska raderas.
"""
import glob, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db, load_saft
from shared import load_config

base = Path(load_config()["base_path"])
hits = glob.glob(str(base / "_history" / "2022" / "*SSP*"))
print("kandidatfiler:", hits)
path = Path(hits[0])
print("torrkör:", path.name)

con = db.connect(read_only=True, role="etl")
try:
    lookup = load_saft.build_orgnr_lookup(con)
    status = load_saft.load_file(con, path, base, None, lookup,
                                 dry_run=True, include_journal=True)
    print("STATUS:", status)
finally:
    con.close()
