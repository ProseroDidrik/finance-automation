"""Enhetstester för SIE-parsning och -validering (load_sie.py).

Körs med stdlib unittest — inga extra beroenden:
    py -m unittest discover -s tests -v
"""
import unittest
from datetime import datetime

import load_sie


class PsaldoDimensionParsing(unittest.TestCase):
    """#PSALDO med objektlista: bara {}-totalen ska laddas, inte dim-splittar.

    Bug: RE_PSALDO matchade \\{[^}]*\\} → fångade både {}-totalen OCH varje
    dimensionssplit-rad → SIE_PSALDO dubbel-/trippelräknades för bolag som
    dim-taggar #PSALDO (23, 75, 186). Se memory reference-sie-psaldo.
    """

    def test_dimension_split_rows_excluded(self):
        text = (
            "#PSALDO 0 202604 6590 {} 857.00\n"
            '#PSALDO 0 202604 6590 {1 "200"} 500.00\n'
            '#PSALDO 0 202604 6590 {1 "201"} 357.00\n'
        )
        parsed = load_sie.parse_sie(text)
        self.assertEqual(parsed["psaldo"], [("202604", "6590", 857.0)])

    def test_empty_dimension_total_still_captured(self):
        text = "#PSALDO 0 202604 6590 {} 857.00\n"
        parsed = load_sie.parse_sie(text)
        self.assertEqual(parsed["psaldo"], [("202604", "6590", 857.0)])


class PsaldoPeriodType(unittest.TestCase):
    """SIE_PSALDO ska taggas period_type='monthly', inte 'ytd'.

    #PSALDO är månadsrörelse. report_pnl.sql 3a-grenen gör YTD-subtraktion på
    'ytd'-rader → felaktig P&L för ~17 SE-bolag. 'monthly' routar raderna till
    3b-grenen som summerar korrekt. Se memory reference-sie-psaldo.
    """

    def test_psaldo_fact_rows_tagged_monthly(self):
        # psaldo_rows: (period, code, name, amount, statement_type, row_index)
        psaldo_rows = [("202604", "6590", "Övriga kostnader", 857.0, "IS", 1)]
        rows = load_sie.psaldo_fact_rows(
            psaldo_rows, company_id=11, currency="SEK",
            rel_src="x.SE", now=datetime(2026, 5, 1))
        # period_type är kolumn 3 (index 2) i fact_balances-inserttupeln
        self.assertEqual([r[2] for r in rows], ["monthly"])

    def test_psaldo_fact_rows_preserve_insert_tuple_shape(self):
        psaldo_rows = [("202604", "6590", "Övriga kostnader", 857.0, "IS", 1)]
        rows = load_sie.psaldo_fact_rows(
            psaldo_rows, company_id=11, currency="SEK",
            rel_src="x.SE", now=datetime(2026, 5, 1))
        # 12 kolumner: company_id, period, period_type, account_code,
        # account_name, amount, currency, statement_type, source_kind,
        # source_file, row_index, loaded_at
        self.assertEqual(
            rows[0],
            (11, "202604", "monthly", "6590", "Övriga kostnader", 857.0,
             "SEK", "IS", "SIE_PSALDO", "x.SE", 1, datetime(2026, 5, 1)),
        )


class PsaldoResConsistency(unittest.TestCase):
    """check_psaldo_vs_res: summa(#PSALDO) per konto = #RES 0.

    #PSALDO är månadsrörelse, #RES 0 är YTD-resultat — de ska stämma. Detta är
    den facit-fria interna avstämningen: SIE-filen är sin egen facit.
    """

    def test_consistent_file_has_no_discrepancies(self):
        parsed = {
            "psaldo": [
                ("202601", "3000", -100.0),
                ("202602", "3000", -100.0),
                ("202603", "3000", -100.0),
            ],
            "res": [("3000", -300.0)],
        }
        self.assertEqual(load_sie.check_psaldo_vs_res(parsed), [])

    def test_psaldo_summing_to_wrong_total_is_flagged(self):
        # #PSALDO felaktigt YTD-staplat (-100/-200/-300) summerar till -600,
        # men #RES 0 = -300 → avvikelse fångas.
        parsed = {
            "psaldo": [
                ("202601", "3000", -100.0),
                ("202602", "3000", -200.0),
                ("202603", "3000", -300.0),
            ],
            "res": [("3000", -300.0)],
        }
        result = load_sie.check_psaldo_vs_res(parsed)
        self.assertEqual(len(result), 1)
        code, sum_psaldo, res_value, diff = result[0]
        self.assertEqual(code, "3000")
        self.assertAlmostEqual(sum_psaldo, -600.0)
        self.assertAlmostEqual(res_value, -300.0)
        self.assertAlmostEqual(diff, -300.0)

    def test_accounts_missing_from_either_side_are_skipped(self):
        parsed = {
            "psaldo": [("202601", "3000", -100.0)],
            "res": [("4000", 50.0)],
        }
        self.assertEqual(load_sie.check_psaldo_vs_res(parsed), [])

    def test_rounding_within_tolerance_not_flagged(self):
        parsed = {
            "psaldo": [("202601", "3000", -100.0), ("202602", "3000", -200.5)],
            "res": [("3000", -300.0)],
        }
        # diff = -0.5, default tol = 1.0 → ingen avvikelse
        self.assertEqual(load_sie.check_psaldo_vs_res(parsed), [])


class VoucherBalance(unittest.TestCase):
    """check_voucher_balance: varje #VER ska balansera (debet = kredit)."""

    def test_balanced_voucher_passes(self):
        parsed = {"vouchers": [
            {"series": "A", "number": "1", "transes": [
                {"account": "1910", "amount": 1000.0},
                {"account": "3000", "amount": -1000.0},
            ]},
        ]}
        self.assertEqual(load_sie.check_voucher_balance(parsed), [])

    def test_unbalanced_voucher_is_flagged(self):
        parsed = {"vouchers": [
            {"series": "A", "number": "7", "transes": [
                {"account": "1910", "amount": 1000.0},
                {"account": "3000", "amount": -940.0},
            ]},
        ]}
        result = load_sie.check_voucher_balance(parsed)
        self.assertEqual(len(result), 1)
        series, number, imbalance = result[0]
        self.assertEqual((series, number), ("A", "7"))
        self.assertAlmostEqual(imbalance, 60.0)

    def test_parsed_sie_voucher_balance(self):
        # Kör mot riktig parse_sie-output (RE_VER + block-hantering + RE_TRANS).
        text = (
            '#VER A 1 20260415 "Test"\n'
            "{\n"
            "#TRANS 1910 {} 1000.00\n"
            "#TRANS 3000 {} -1000.00\n"
            "}\n"
        )
        parsed = load_sie.parse_sie(text, with_journal=True)
        self.assertEqual(load_sie.check_voucher_balance(parsed), [])


class PsaldoDimCoverage(unittest.TestCase):
    """psaldo_dim_coverage: spot-check-diagnostik för Bug 2-fixens säkerhet.

    Säkerställer att {}-only-regexen inte tappar konton som saknar {}-total.
    """

    def test_all_accounts_have_total_row(self):
        # Konto 6590 dim-taggat med två dimensionstyper (1 och 6) — varje typ
        # återger hela beloppet. Båda kontona har {}-total → inget tappas.
        text = (
            "#PSALDO 0 202604 6590 {} 857.00\n"
            '#PSALDO 0 202604 6590 {1 "200"} 857.00\n'
            '#PSALDO 0 202604 6590 {6 "U250"} 857.00\n'
            "#PSALDO 0 202604 3000 {} -1000.00\n"
        )
        cov = load_sie.psaldo_dim_coverage(text)
        self.assertEqual(cov["lost_accounts"], [])
        self.assertEqual(cov["total_row_count"], 2)
        self.assertEqual(cov["all_psaldo_accounts"], 2)

    def test_account_with_only_dim_rows_is_lost(self):
        text = (
            "#PSALDO 0 202604 6590 {} 857.00\n"
            '#PSALDO 0 202604 7000 {1 "200"} 300.00\n'
            '#PSALDO 0 202604 7000 {6 "U1"} 300.00\n'
        )
        cov = load_sie.psaldo_dim_coverage(text)
        self.assertEqual(cov["lost_accounts"], ["7000"])
        self.assertEqual(cov["total_row_count"], 1)


class SieDimAnalysisRows(unittest.TestCase):
    NOW = datetime(2026, 5, 29)

    def test_types_and_members_from_two_lists(self):
        dims = [("1", "Avdelning"), ("6", "Projekt")]
        objekt = [("1", "100", "Adm"), ("1", "200", "Salg"), ("6", "9000300", "PX")]
        type_rows, member_rows = load_sie.sie_dim_analysis_rows(
            dims, objekt, company_id=32, now=self.NOW)
        self.assertEqual({r[2] for r in type_rows}, {"1", "6"})
        self.assertEqual(len(type_rows), 2)
        self.assertIn((32, "SIE", "1", "Avdelning", self.NOW), type_rows)
        self.assertEqual(len(member_rows), 3)
        self.assertIn((32, "SIE", "1", "100", "Adm", self.NOW), member_rows)

    def test_empty_lists(self):
        self.assertEqual(load_sie.sie_dim_analysis_rows([], [], 1, self.NOW), ([], []))


class VoucherAnalysisRows(unittest.TestCase):
    NOW = datetime(2026, 5, 29)

    def _parsed(self):
        return {
            "konto": {"7830": "Avskrivningar"},
            "vouchers": [{
                "series": "IN26", "number": "1", "date": "20260131", "text": "x",
                "transes": [
                    {"line_no": 1, "account": "7830", "amount": 1247.27,
                     "trans_text": "a", "quantity": None,
                     "analysis": [("1", "100"), ("6", "9000300")]},
                    {"line_no": 2, "account": "1209", "amount": -1247.27,
                     "trans_text": "b", "quantity": None, "analysis": []},
                ],
            }],
        }

    def test_analysis_rows_share_voucher_period(self):
        rows, analysis_rows, periods, skipped = load_sie.vouchers_to_journal_rows(
            self._parsed(), company_id=32, currency="SEK",
            rel_src="x.se", now=self.NOW)
        self.assertEqual(periods, {"202601"})
        self.assertEqual(len(analysis_rows), 2)
        # tuple: (company_id, period, series, voucher_number, line_no,
        #         account_code, analysis_type, analysis_id, amount, currency,
        #         source_file, loaded_at)
        self.assertEqual(analysis_rows[0],
            (32, "202601", "IN26", "1", 1, "7830", "1", "100",
             1247.27, "SEK", "x.se", self.NOW))
        self.assertEqual(analysis_rows[1][6:9], ("6", "9000300", 1247.27))
        self.assertTrue({r[1] for r in analysis_rows} <= periods)

    def test_cutoff_skips_journal_and_analysis(self):
        parsed = self._parsed()
        parsed["vouchers"][0]["date"] = "20260531"
        rows, analysis_rows, periods, skipped = load_sie.vouchers_to_journal_rows(
            parsed, 32, "SEK", "x.se", self.NOW, period_cutoff="202604")
        self.assertEqual(skipped, 1)
        self.assertEqual(rows, [])
        self.assertEqual(analysis_rows, [])


if __name__ == "__main__":
    unittest.main()
