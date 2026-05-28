"""Tester för den delade SIE-parsern (sie_parser.py).

Låser det header-kontrakt som både load_sie.py och process_sweden.py bygger på
(orgnr/rar/fnamn) samt CP437-teckenhantering. Körs med stdlib unittest:
    py -m unittest discover -s tests -v
"""
import tempfile
import unittest
from pathlib import Path

import sie_parser


class HeaderFields(unittest.TestCase):
    """orgnr/rar/fnamn — fälten process_sweden använder för rename + periodkoll."""

    def test_orgnr_unquoted_swedish(self):
        parsed = sie_parser.parse_sie("#ORGNR 556071-2340\n")
        self.assertEqual(parsed["orgnr"], "556071-2340")

    def test_orgnr_quoted_with_spaces_norwegian(self):
        # Norska Global-exporter: citerad sträng med mellanslag och MVA-suffix.
        parsed = sie_parser.parse_sie('#ORGNR "NO 971199954 MVA"\n')
        self.assertEqual(parsed["orgnr"], "NO 971199954 MVA")

    def test_rar0_start_end(self):
        parsed = sie_parser.parse_sie("#RAR 0 20260101 20261231\n")
        self.assertEqual((parsed["rar_start"], parsed["rar_end"]),
                         ("20260101", "20261231"))

    def test_fnamn_quoted(self):
        parsed = sie_parser.parse_sie('#FNAMN "Axlås Solidlås AB"\n')
        self.assertEqual(parsed["fnamn"], "Axlås Solidlås AB")


class SeriesVariants(unittest.TestCase):
    """#VER-serie får vara ociterad bokstav/siffra eller citerad alfanumerisk
    (SIE 4B §5.7 — citat krävs bara vid mellanslag). Olika exportörer skiljer sig."""

    def _first_voucher(self, text):
        parsed = sie_parser.parse_sie(text, with_journal=True)
        return parsed["vouchers"][0]

    def test_unquoted_letter_series_fortnox(self):
        v = self._first_voucher('#VER A 1 20260101 "Monthly fee" 20260107\n{\n'
                                 "#TRANS 2890 {} -73.75\n#TRANS 6991 {} 73.75\n}\n")
        self.assertEqual((v["series"], v["number"]), ("A", "1"))
        self.assertEqual(len(v["transes"]), 2)

    def test_quoted_series_hantverksdata(self):
        v = self._first_voucher('#VER "IN26" 1 20260131 "Avskrivning"\n{\n'
                                 '#TRANS 7830 {"1" "100"} 1000.00\n'
                                 "#TRANS 1209 {} -1000.00\n}\n")
        self.assertEqual((v["series"], v["number"]), ("IN26", "1"))


class Cp437Encoding(unittest.TestCase):
    """SIE är CP437-kodat (PC8). å/ä/ö måste läsas rätt via fallback-kedjan."""

    def test_cp437_swedish_chars_roundtrip(self):
        # CP437-bytes för å/ä/ö är ogiltig UTF-8 → fallback faller till cp437.
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.se"
            p.write_bytes('#FNAMN "Råäö Säkerhet AB"\n'.encode("cp437"))
            text = sie_parser.read_text_with_fallback(p)
        self.assertEqual(sie_parser.parse_sie(text)["fnamn"], "Råäö Säkerhet AB")


if __name__ == "__main__":
    unittest.main()
