"""Råinspektion: har SAF-T-linjerna för 158/6010 olika ValueDate per månad
medan TransactionDate är samma (jan)? Om ja → vår ETL periodiserar på fel fält.

Dumpar RÅ XML-struktur (alla datum-relaterade taggar) utan tolkning.
"""
import sys
import json
import xml.etree.ElementTree as ET
from pathlib import Path

cfg = json.loads(Path("config.json").read_text(encoding="utf-8"))
base = Path(cfg["base_path"])
xml_path = base / "extracted" / "202604" / "Norway" / "158_Asker_TT_SAF-T_2026-12.xml"

# Namespace-detektion
def detect_ns(path):
    for _, elem in ET.iterparse(str(path), events=("start",)):
        tag = elem.tag
        if "}" in tag:
            return tag.split("}", 1)[0][1:]
        return ""
    return ""

ns = detect_ns(xml_path)
print(f"namespace: {ns!r}")
print(f"fil: {xml_path.name}\n")

def t(parent, name):
    el = parent.find(f"{{{ns}}}{name}") if ns else parent.find(name)
    return el.text if el is not None else None

ctx = ET.iterparse(str(xml_path), events=("end",))
shown = 0
TARGET = "6010"
for event, elem in ctx:
    tag = elem.tag.split("}", 1)[-1] if "}" in elem.tag else elem.tag
    if tag == "Journal":
        for tx in elem.findall(f"{{{ns}}}Transaction"):
            tx_id = t(tx, "TransactionID")
            tx_date = t(tx, "TransactionDate")
            tx_posting = t(tx, "GLPostingDate")  # finns ibland
            tx_sysentry = t(tx, "SystemEntryDate")
            lines = tx.findall(f"{{{ns}}}Line")
            # Har denna transaktion någon 6010-linje?
            hit = any(t(ln, "AccountID") == TARGET for ln in lines)
            if not hit:
                continue
            print(f"--- Transaction {tx_id} ---")
            print(f"    TransactionDate = {tx_date}")
            print(f"    GLPostingDate   = {tx_posting}")
            print(f"    SystemEntryDate = {tx_sysentry}")
            for ln in lines:
                acc = t(ln, "AccountID")
                if acc != TARGET:
                    continue
                vdate = t(ln, "ValueDate")
                rec = t(ln, "RecordID")
                desc = t(ln, "Description")
                deb = ln.find(f"{{{ns}}}DebitAmount")
                cre = ln.find(f"{{{ns}}}CreditAmount")
                deb_amt = t(deb, "Amount") if deb is not None else None
                cre_amt = t(cre, "Amount") if cre is not None else None
                print(f"      Line acc={acc} ValueDate={vdate} D={deb_amt} C={cre_amt} desc={desc!r}")
            shown += 1
            if shown >= 15:
                sys.exit(0)
        elem.clear()
