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


class TransDimensions(unittest.TestCase):
    """#DIM/#OBJEKT persisteras inte — men objektlistan {…} i #TRANS får ALDRIG
    läcka in i beloppet. Låser beteendet som oraklet bevisat (0 obalanser över
    dim-tunga Hantverksdata-filer)."""

    def _transes(self, ver_text):
        return sie_parser.parse_sie(ver_text, with_journal=True)["vouchers"][0]["transes"]

    def test_multidim_brace_does_not_leak_into_amount(self):
        # Hantverksdata: flera dim-par i braces; tal i objektlistan (9000300)
        # får inte förväxlas med beloppet (1247.27).
        t = self._transes(
            '#VER "IN26" 1 20260131 "Avskrivning"\n{\n'
            '\t#TRANS 7830 {"1" "100" "2" "300" "6" "9000300" } 1247.27 20260131 "Avskr" 1\n'
            "\t#TRANS 1209 {} -1247.27 20260131 \"Ack\" 2\n}\n")
        self.assertEqual(t[0]["account"], "7830")
        self.assertEqual(t[0]["amount"], 1247.27)
        self.assertEqual(t[0]["quantity"], 1.0)

    def test_empty_brace_amount(self):
        t = self._transes('#VER A 1 20260101 "x"\n{\n#TRANS 1209 {} -1000.00\n'
                          "#TRANS 7830 {} 1000.00\n}\n")
        self.assertEqual(t[0]["amount"], -1000.00)

    def test_tab_separated_visma_net(self):
        # Visma.net: tab-indenterad, tab-separerad, tom brace.
        t = self._transes('#VER\tAP\t002764\t20260102\t"AP/002764/"\n{\n'
                           '\t#TRANS\t1940\t{}\t-89069.57\t20260102\t"CHK"\n'
                           '\t#TRANS\t2440\t{}\t89069.57\t20260102\t"CHK"\n}\n')
        self.assertEqual(t[0]["account"], "1940")
        self.assertEqual(t[0]["amount"], -89069.57)

    def test_voucher_with_dims_still_balances(self):
        parsed = sie_parser.parse_sie(
            '#VER "IN26" 1 20260131 "x"\n{\n'
            '\t#TRANS 7830 {"6" "9000300"} 1247.27\n'
            "\t#TRANS 1209 {} -1247.27\n}\n", with_journal=True)
        self.assertEqual(sie_parser.check_voucher_balance(parsed), [])


class FormatAndCurrency(unittest.TestCase):
    """#FORMAT (PC8/CP437) och #VALUTA fångas så grindar/loader kan använda dem.

    #VALUTA default = SEK i SIE (svenskt format); enstaka norska SIE-bolag
    deklarerar NOK och ska då inte hårdkodas som SEK."""

    def test_format_captured(self):
        self.assertEqual(sie_parser.parse_sie("#FORMAT PC8\n")["format"], "PC8")

    def test_format_absent_is_none(self):
        self.assertIsNone(sie_parser.parse_sie("#ORGNR 556071-2340\n")["format"])

    def test_currency_captured(self):
        self.assertEqual(sie_parser.parse_sie("#VALUTA NOK\n")["currency"], "NOK")

    def test_currency_absent_is_none(self):
        self.assertIsNone(sie_parser.parse_sie("#ORGNR 556071-2340\n")["currency"])


class ValidationGate(unittest.TestCase):
    """validate_sie: bypassbara datakvalitetsgrindar (#FORMAT + verifikatbalans).
    Returnerar lista med blockerande fel; tom lista = OK att ladda."""

    BALANCED = ('#FORMAT PC8\n#ORGNR 556071-2340\n'
                '#VER A 1 20260101 "x"\n{\n#TRANS 1910 {} 100.00\n'
                "#TRANS 3000 {} -100.00\n}\n")

    def test_valid_file_passes(self):
        parsed = sie_parser.parse_sie(self.BALANCED, with_journal=True)
        self.assertEqual(sie_parser.validate_sie(parsed), [])

    def test_missing_format_blocks(self):
        parsed = sie_parser.parse_sie('#ORGNR 556071-2340\n', with_journal=True)
        errs = sie_parser.validate_sie(parsed)
        self.assertTrue(any("FORMAT" in e for e in errs))

    def test_wrong_format_blocks(self):
        parsed = sie_parser.parse_sie('#FORMAT UTF8\n#ORGNR 1\n', with_journal=True)
        errs = sie_parser.validate_sie(parsed)
        self.assertTrue(any("FORMAT" in e for e in errs))

    def test_unbalanced_voucher_blocks(self):
        text = ('#FORMAT PC8\n#ORGNR 1\n#VER A 7 20260101 "x"\n{\n'
                "#TRANS 1910 {} 100.00\n#TRANS 3000 {} -40.00\n}\n")
        parsed = sie_parser.parse_sie(text, with_journal=True)
        errs = sie_parser.validate_sie(parsed)
        self.assertTrue(any("verifikat" in e.lower() for e in errs))

    def test_no_journal_skips_voucher_check(self):
        # Utan journal kan vi inte kolla verifikatbalans — bara #FORMAT gäller.
        parsed = sie_parser.parse_sie(self.BALANCED, with_journal=False)
        self.assertEqual(sie_parser.validate_sie(parsed, with_journal=False), [])


class DimDeclarations(unittest.TestCase):
    def test_dim_and_objekt_parsed(self):
        p = sie_parser.parse_sie(
            '#DIM 1 "Avdelning"\n#DIM 6 "Projekt"\n'
            '#OBJEKT 1 "100" "Administration"\n'
            '#OBJEKT 6 "9000300" "Projekt X"\n')
        self.assertIn(("1", "Avdelning"), p["dims"])
        self.assertIn(("6", "Projekt"), p["dims"])
        self.assertIn(("1", "100", "Administration"), p["objekt"])
        self.assertIn(("6", "9000300", "Projekt X"), p["objekt"])

    def test_dim_objekt_absent_is_empty(self):
        p = sie_parser.parse_sie('#ORGNR 556071-2340\n')
        self.assertEqual(p["dims"], [])
        self.assertEqual(p["objekt"], [])

    def test_objekt_unquoted_objektnr(self):
        p = sie_parser.parse_sie('#OBJEKT 1 100 "Adm"\n')
        self.assertIn(("1", "100", "Adm"), p["objekt"])


class TransAnalysis(unittest.TestCase):
    def _transes(self, ver_text):
        return sie_parser.parse_sie(ver_text, with_journal=True)["vouchers"][0]["transes"]

    def test_multidim_pairs_extracted(self):
        t = self._transes(
            '#VER "IN26" 1 20260131 "x"\n{\n'
            '\t#TRANS 7830 {"1" "100" "6" "9000300"} 1247.27 20260131 "Avskr" 1\n'
            '\t#TRANS 1209 {} -1247.27\n}\n')
        self.assertEqual(t[0]["analysis"], [("1", "100"), ("6", "9000300")])
        self.assertEqual(t[0]["amount"], 1247.27)
        self.assertEqual(t[0]["quantity"], 1.0)
        self.assertEqual(t[1]["analysis"], [])

    def test_unquoted_dim_tokens(self):
        t = self._transes('#VER A 1 20260101 "x"\n{\n#TRANS 5420 {1 2} 333.7\n}\n')
        self.assertEqual(t[0]["analysis"], [("1", "2")])

    def test_odd_token_count_drops_dangling(self):
        t = self._transes('#VER A 1 20260101 "x"\n{\n#TRANS 5420 {1 "100" 2} 333.7\n}\n')
        self.assertEqual(t[0]["analysis"], [("1", "100")])
        self.assertEqual(t[0]["amount"], 333.7)


if __name__ == "__main__":
    unittest.main()
