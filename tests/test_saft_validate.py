"""Tester för XSD-valideringsgrinden (saft_parser.validate_xsd).

Grinden validerar SAF-T mot vendorad XSD via lxml.etree.XMLSchema (xmllint finns
inte på Windows-maskinen; lxml är redan beroende, samma XSD-standard). Bara
NO 1.30 har vendorad XSD → övriga versioner/namespace WARN-skippas (best-effort).

Riktiga 1.30-filers schemaföljsamhet (status 'valid') verifieras separat över
golden-filerna i tests/test_saft_oracle.py-grannen test_saft_xsd_compliance.

    py -m unittest tests.test_saft_validate -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling-fixturer

import saft_parser
from test_saft_parser import NO_XML, DK_XML

NO_120 = """<?xml version="1.0" encoding="UTF-8"?>
<AuditFile xmlns="urn:StandardAuditFile-Taxation-Financial:NO">
  <Header><AuditFileVersion>1.20</AuditFileVersion><AuditFileCountry>NO</AuditFileCountry></Header>
</AuditFile>
"""

UNKNOWN_NS = """<?xml version="1.0" encoding="UTF-8"?>
<Foo xmlns="urn:example:not-saft"><Bar>x</Bar></Foo>
"""


def _write(text: str) -> Path:
    d = tempfile.mkdtemp()
    p = Path(d) / "saft.xml"
    p.write_text(text, encoding="utf-8")
    return p


class HeaderMeta(unittest.TestCase):
    def test_no_130_meta(self):
        ns, country, version = saft_parser._quick_header_meta(_write(NO_XML))
        self.assertEqual(country, "NO")
        self.assertEqual(version, "1.30")

    def test_dk_meta(self):
        ns, country, version = saft_parser._quick_header_meta(_write(DK_XML))
        self.assertEqual(country, "DK")


class XsdGateRouting(unittest.TestCase):
    """Versions/namespace-routning: bara NO 1.30 valideras, övrigt skippas."""

    def test_120_skipped(self):
        status, errors = saft_parser.validate_xsd(_write(NO_120))
        self.assertEqual(status, "skipped")

    def test_dk_skipped(self):
        status, errors = saft_parser.validate_xsd(_write(DK_XML))
        self.assertEqual(status, "skipped")

    def test_unknown_namespace_skipped(self):
        status, errors = saft_parser.validate_xsd(_write(UNKNOWN_NS))
        self.assertEqual(status, "skipped")


class XsdGateValidation(unittest.TestCase):
    def test_invalid_130_reports_errors(self):
        # NO_XML är ett minimalt 1.30-skelett som INTE är schemakomplett.
        status, errors = saft_parser.validate_xsd(_write(NO_XML))
        self.assertEqual(status, "invalid")
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
