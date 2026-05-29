"""Pure-unit för analys-grupperingen i load_saft.py (ingen DB).

group_analysis_by_period grupperar analystupler per ValueDate-härledd period via
line_rows — kärnan i historik-backfillen. Bekräftar att perioden ärvs från
ValueDate (b711832-skydd) och att cutoff/multi-block hanteras."""
import unittest
from datetime import date, datetime

import load_saft

NOW = datetime(2026, 5, 29)


def _line(value_date, transaction_date, analysis, debit=100.0):
    return {
        "journal_id": "J1", "journal_desc": "d",
        "transaction_id": "T1", "transaction_date": transaction_date,
        "transaction_desc": "td", "value_date": value_date,
        "line_no": 1, "record_id": "1", "account_code": "3000",
        "line_desc": "x", "debit": debit, "credit": 0.0,
        "analysis": analysis,
    }


class GroupAnalysisByPeriod(unittest.TestCase):
    def test_groups_by_value_date_period(self):
        lines = [
            _line(date(2024, 3, 15), date(2024, 1, 31), [("DEP", "3")]),
            _line(date(2024, 1, 5), date(2024, 1, 5), [("DEP", "1")]),
        ]
        out = load_saft.group_analysis_by_period(
            lines, company_id=9, currency="NOK", rel_src="x.xml",
            now=NOW, fallback_period="202412")
        self.assertEqual(set(out), {"202403", "202401"})
        self.assertEqual(out["202403"][0][6], "DEP")   # analysis_type
        self.assertEqual(out["202403"][0][1], "202403")  # period i tupeln

    def test_cutoff_excludes_later_period(self):
        lines = [_line(date(2025, 1, 10), date(2025, 1, 10), [("DEP", "1")])]
        out = load_saft.group_analysis_by_period(
            lines, 9, "NOK", "x.xml", NOW,
            fallback_period="202412", period_cutoff="202412")
        self.assertEqual(out, {})   # jp 202501 > cutoff 202412 → skippad

    def test_multi_block_grouped_under_same_period(self):
        lines = [_line(date(2024, 6, 2), date(2024, 6, 2),
                       [("DEP", "3"), ("PRO", "1")])]
        out = load_saft.group_analysis_by_period(
            lines, 9, "NOK", "x.xml", NOW, fallback_period="202412")
        self.assertEqual(len(out["202406"]), 2)
        self.assertEqual([t[6] for t in out["202406"]], ["DEP", "PRO"])

    def test_line_without_analysis_absent(self):
        lines = [_line(date(2024, 6, 2), date(2024, 6, 2), [])]
        out = load_saft.group_analysis_by_period(
            lines, 9, "NOK", "x.xml", NOW, fallback_period="202412")
        self.assertEqual(out, {})


if __name__ == "__main__":
    unittest.main()
