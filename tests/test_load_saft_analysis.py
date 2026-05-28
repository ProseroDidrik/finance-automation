"""Pure-unit-tester för analys-radbyggarna i load_saft.py (ingen DB).

Speglar mönstret i test_load_sie.py (psaldo_fact_rows). Den kritiska invarianten:
analysradens period = journalradens period = ValueDate per linje (skydd mot
b711832-regression i dimensionslagret).
"""
import unittest
from datetime import date, datetime

import load_saft

NOW = datetime(2026, 5, 28)


def _line(value_date, transaction_date, analysis):
    return {
        "journal_id": "J1", "journal_desc": "d",
        "transaction_id": "T1", "transaction_date": transaction_date,
        "transaction_desc": "td", "value_date": value_date,
        "line_no": 1, "record_id": "1", "account_code": "3000",
        "line_desc": "x", "debit": 100.0, "credit": 0.0,
        "analysis": analysis,
    }


class LineRowsPeriodBinding(unittest.TestCase):
    def test_period_from_value_date_not_transaction_date(self):
        # Tripletex-divergens: bokförd i jan, ValueDate i mars.
        line = _line(date(2026, 3, 15), date(2026, 1, 31), [("DEP", "3")])
        jt, ats, jp, skipped = load_saft.line_rows(
            line, company_id=9, currency="NOK", rel_src="x.xml",
            now=NOW, fallback_period="202604")
        self.assertEqual(jp, "202603")
        self.assertEqual(jt[1], "202603")           # journaltupelns period
        self.assertEqual(ats[0][1], "202603")       # analystupelns period == samma

    def test_fallback_to_transaction_date_when_no_value_date(self):
        line = _line(None, date(2026, 1, 31), [("DEP", "3")])
        jt, ats, jp, skipped = load_saft.line_rows(
            line, 9, "NOK", "x.xml", NOW, "202604")
        self.assertEqual(jp, "202601")
        self.assertEqual(ats[0][1], "202601")

    def test_multi_block_explosion_same_amount_same_period(self):
        line = _line(date(2026, 4, 2), date(2026, 4, 2), [("DEP", "3"), ("PRO", "1")])
        jt, ats, jp, skipped = load_saft.line_rows(
            line, 9, "NOK", "x.xml", NOW, "202604")
        self.assertEqual(len(ats), 2)
        self.assertEqual([a[6] for a in ats], ["DEP", "PRO"])     # analysis_type
        self.assertEqual([a[8] for a in ats], [100.0, 100.0])     # amount = debit-credit
        self.assertTrue(all(a[1] == "202604" for a in ats))

    def test_cutoff_skips_line_and_analysis(self):
        line = _line(date(2026, 5, 10), date(2026, 5, 10), [("DEP", "3")])
        jt, ats, jp, skipped = load_saft.line_rows(
            line, 9, "NOK", "x.xml", NOW, "202604", period_cutoff="202604")
        self.assertTrue(skipped)
        self.assertIsNone(jt)
        self.assertEqual(ats, [])


class DimAnalysisRows(unittest.TestCase):
    def test_dedup_types_and_members(self):
        analysis_types = [
            ("DEP", "Avdeling", "1", "Adm"),
            ("DEP", "Avdeling", "2", "Salg"),
            ("PRO", "Prosjekt", "1", "P1"),
        ]
        type_rows, member_rows = load_saft.dim_analysis_rows(
            analysis_types, company_id=9, now=NOW)
        self.assertEqual({r[2] for r in type_rows}, {"DEP", "PRO"})
        self.assertEqual(len(type_rows), 2)         # DEP deduplicerad
        self.assertEqual(len(member_rows), 3)
        # member-tupel: (company_id, source_format, analysis_type, analysis_id, desc, now)
        self.assertIn((9, "SAFT", "DEP", "1", "Adm", NOW), member_rows)


if __name__ == "__main__":
    unittest.main()
