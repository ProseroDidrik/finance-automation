"""Punkt 2: ladda bolag 104:s 2022 SAF-T (journal+balans+analys) till prod och
fyll gapet. Använder den FY-fixade load_file (denna gren). Idempotent: re-deriverar
2022-analysen ur samma fil → föräldralösheten försvinner (analys får matchande
journal). ETL-roll, en transaktion.
"""
import glob, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db, load_saft
from shared import load_config, log

base = Path(load_config()["base_path"])
path = Path(glob.glob(str(base / "_history" / "2022" / "inl SSP*"))[0])
log("START", "load_104", f"{path.name}")

con = db.connect(role="etl")
try:
    try:
        db.init_schema(con)
    except Exception as e:
        if "InsufficientPrivilege" in type(e).__name__ or "permission denied" in str(e).lower():
            con.raw.rollback()
        else:
            raise
    lookup = load_saft.build_orgnr_lookup(con)
    status = load_saft.load_file(con, path, base, None, lookup,
                                 dry_run=False, include_journal=True)
    log("DONE", "load_104", f"status={status}")
finally:
    con.close()
