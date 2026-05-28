"""Test-tids schemaföljsamhet för riktiga NO SAF-T-filer (Etapp 4).

Realiserar pivotens "xsdata/XSD som test-tids-validator": över de riktiga
filerna bevisas att

  (1) alla NO 1.30-filer validerar mot vendorad XSD (saft_parser.validate_xsd),
  (2) NO 1.20 + DK saknar XSD och 'skipped' (best-effort),
  (3) de vendorade xsdata-klasserna parsar både en 1.30- och en 1.20-fil rent
      (lenient) — bevisar att grouping-patchen matchar riktig data.

Runtime-parsningen är manuell iterparse (xsdata mätte ~11x för långsam); detta
test är säkerhetsnätet som ändå håller koden ärlig mot schemat.

Kräver config.json + riktiga filer under base_path. xsdata-parsningen är tung →
välj minsta filen per version för att hålla testet snabbt.

    py -m unittest tests.test_saft_xsd_compliance -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import saft_parser


def _no_files():
    try:
        from shared import load_config
        base = Path(load_config()["base_path"])
        d = base / "extracted" / "202604" / "Norway"
        return sorted(d.glob("*.xml")) if d.exists() else []
    except Exception:
        return []


_FILES = _no_files()


def _by_version():
    """{version: [paths]} för NO-filerna."""
    out: dict[str, list[Path]] = {}
    for f in _FILES:
        _ns, _c, ver = saft_parser._quick_header_meta(f)
        out.setdefault(ver, []).append(f)
    return out


@unittest.skipUnless(_FILES, "riktiga NO SAF-T-filer saknas under base_path")
class XsdCompliance(unittest.TestCase):
    def setUp(self):
        self.by_ver = _by_version()

    def test_all_130_files_valid(self):
        bad = []
        for f in self.by_ver.get("1.30", []):
            status, errors = saft_parser.validate_xsd(f)
            if status != "valid":
                bad.append(f"{f.name}: {status} ({errors[:1]})")
        self.assertEqual(bad, [], "1.30-filer som inte validerar:\n" + "\n".join(bad))

    def test_120_files_skipped(self):
        for f in self.by_ver.get("1.20", []):
            status, _ = saft_parser.validate_xsd(f)
            self.assertEqual(status, "skipped", f"{f.name} borde skippas (ingen 1.20-XSD)")

    def test_xsdata_classes_match_real_130(self):
        files = self.by_ver.get("1.30", [])
        if not files:
            self.skipTest("ingen 1.30-fil")
        smallest = min(files, key=lambda f: f.stat().st_size)
        self._assert_xsdata_parses(smallest)

    def test_xsdata_classes_match_real_120(self):
        files = self.by_ver.get("1.20", [])
        if not files:
            self.skipTest("ingen 1.20-fil")
        smallest = min(files, key=lambda f: f.stat().st_size)
        self._assert_xsdata_parses(smallest)

    def _assert_xsdata_parses(self, path: Path):
        from xsdata.formats.dataclass.parsers import XmlParser
        from xsdata.formats.dataclass.parsers.config import ParserConfig
        from saft_schema_no import AuditFile
        parser = XmlParser(config=ParserConfig(fail_on_unknown_properties=False))
        af = parser.parse(str(path), AuditFile)
        self.assertIsNotNone(af.header, f"{path.name}: header band inte")
        self.assertTrue(af.master_files.general_ledger_accounts.account,
                        f"{path.name}: inga konton band")


if __name__ == "__main__":
    unittest.main()
