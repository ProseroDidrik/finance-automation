"""Tester för den delade SAF-T-parsern (saft_parser.py).

Låser parse-kontraktet (header/accounts/journal) och teckenkonventionen. Körs
med stdlib unittest utan databas — inline-XML skrivs till temp-fil:
    py -m unittest tests.test_saft_parser -v

Den fullständiga byte-för-byte-likvärdigheten mot tidigare load_saft-parsning
verifieras separat av regressions-orakelet (tests/test_saft_oracle.py).
"""
import tempfile
import unittest
from datetime import date
from pathlib import Path

import saft_parser

# Minimal NO SAF-T 1.30. Company OCH AuditFileSender har RegistrationNumber —
# orgnr måste tas från Company (916059701), aldrig sender (999888777).
NO_XML = """<?xml version="1.0" encoding="UTF-8"?>
<AuditFile xmlns="urn:StandardAuditFile-Taxation-Financial:NO">
  <Header>
    <AuditFileVersion>1.30</AuditFileVersion>
    <AuditFileCountry>NO</AuditFileCountry>
    <AuditFileDateCreated>2026-05-01</AuditFileDateCreated>
    <SoftwareCompanyName>X</SoftwareCompanyName>
    <SoftwareID>TestSW</SoftwareID>
    <SoftwareVersion>1</SoftwareVersion>
    <Company>
      <RegistrationNumber>916059701</RegistrationNumber>
      <Name>Test Company AS</Name>
    </Company>
    <DefaultCurrencyCode>NOK</DefaultCurrencyCode>
    <SelectionCriteria>
      <SelectionStartDate>2026-01-01</SelectionStartDate>
      <SelectionEndDate>2026-04-30</SelectionEndDate>
      <PeriodStart>1</PeriodStart>
      <PeriodStartYear>2026</PeriodStartYear>
      <PeriodEnd>4</PeriodEnd>
      <PeriodEndYear>2026</PeriodEndYear>
    </SelectionCriteria>
    <TaxAccountingBasis>A</TaxAccountingBasis>
    <AuditFileSender>
      <RegistrationNumber>999888777</RegistrationNumber>
      <Name>Accounting Office AS</Name>
    </AuditFileSender>
  </Header>
  <MasterFiles>
    <GeneralLedgerAccounts>
      <Account>
        <AccountID>1500</AccountID>
        <AccountDescription>Kundefordringer</AccountDescription>
        <GroupingCategory>fordring</GroupingCategory>
        <GroupingCode>1500</GroupingCode>
        <AccountType>GL</AccountType>
        <ClosingDebitBalance>1000.00</ClosingDebitBalance>
      </Account>
      <Account>
        <AccountID>3000</AccountID>
        <AccountDescription>Salgsinntekt</AccountDescription>
        <GroupingCategory>inntekt</GroupingCategory>
        <GroupingCode>3000</GroupingCode>
        <AccountType>GL</AccountType>
        <ClosingCreditBalance>1000.00</ClosingCreditBalance>
      </Account>
    </GeneralLedgerAccounts>
  </MasterFiles>
  <GeneralLedgerEntries>
    <NumberOfEntries>1</NumberOfEntries>
    <TotalDebit>1000.00</TotalDebit>
    <TotalCredit>1000.00</TotalCredit>
    <Journal>
      <JournalID>J1</JournalID>
      <Description>Salg</Description>
      <Type>GL</Type>
      <Transaction>
        <TransactionID>T1</TransactionID>
        <Period>3</Period>
        <PeriodYear>2026</PeriodYear>
        <TransactionDate>2026-01-15</TransactionDate>
        <Description>Faktura</Description>
        <SystemEntryDate>2026-01-15</SystemEntryDate>
        <GLPostingDate>2026-01-15</GLPostingDate>
        <Line>
          <RecordID>R1</RecordID>
          <AccountID>1500</AccountID>
          <ValueDate>2026-03-31</ValueDate>
          <Description>Kundefordring</Description>
          <DebitAmount><Amount>1000.00</Amount></DebitAmount>
        </Line>
        <Line>
          <RecordID>R2</RecordID>
          <AccountID>3000</AccountID>
          <Description>Inntekt</Description>
          <CreditAmount><Amount>1000.00</Amount></CreditAmount>
        </Line>
      </Transaction>
    </Journal>
  </GeneralLedgerEntries>
</AuditFile>
"""

# Minimal DK SAF-T (annan namespace, DK-kontogränser).
DK_XML = """<?xml version="1.0" encoding="UTF-8"?>
<AuditFile xmlns="urn:StandardAuditFile-Taxation-Financial:DK">
  <Header>
    <Company>
      <RegistrationNumber>29 14 36 25</RegistrationNumber>
      <Name>Dansk Selskab ApS</Name>
    </Company>
    <DefaultCurrencyCode>DKK</DefaultCurrencyCode>
    <SelectionCriteria>
      <SelectionStartDate>2026-01-01</SelectionStartDate>
      <SelectionEndDate>2026-04-30</SelectionEndDate>
    </SelectionCriteria>
  </Header>
  <MasterFiles>
    <GeneralLedgerAccounts>
      <Account>
        <AccountID>1000</AccountID>
        <AccountDescription>Omsaetning</AccountDescription>
        <ClosingCreditBalance>5000.00</ClosingCreditBalance>
      </Account>
      <Account>
        <AccountID>5800</AccountID>
        <AccountDescription>Bank</AccountDescription>
        <ClosingDebitBalance>5000.00</ClosingDebitBalance>
      </Account>
    </GeneralLedgerAccounts>
  </MasterFiles>
</AuditFile>
"""


def _write(text: str) -> Path:
    d = tempfile.mkdtemp()
    p = Path(d) / "saft.xml"
    p.write_text(text, encoding="utf-8")
    return p


class NamespaceDetection(unittest.TestCase):
    def test_no_namespace(self):
        parsed = saft_parser.parse_saft(_write(NO_XML))
        self.assertEqual(parsed["ns"], "urn:StandardAuditFile-Taxation-Financial:NO")
        self.assertEqual(parsed["country"], "NO")

    def test_dk_namespace(self):
        parsed = saft_parser.parse_saft(_write(DK_XML))
        self.assertEqual(parsed["country"], "DK")


class StatementType(unittest.TestCase):
    """NO: 1/2=BS, 3-9=IS. DK: 4-siffrigt prefix ≤4999=IS, ≥5000=BS."""

    def test_no_balance_vs_income(self):
        self.assertEqual(saft_parser.statement_type_from_code("1500", "NO"), "BS")
        self.assertEqual(saft_parser.statement_type_from_code("2400", "NO"), "BS")
        self.assertEqual(saft_parser.statement_type_from_code("3000", "NO"), "IS")
        self.assertEqual(saft_parser.statement_type_from_code("8990", "NO"), "IS")

    def test_dk_boundary(self):
        self.assertEqual(saft_parser.statement_type_from_code("4999", "DK"), "IS")
        self.assertEqual(saft_parser.statement_type_from_code("5000", "DK"), "BS")

    def test_dk_long_account_uses_prefix4(self):
        # 6-siffrigt: klassas på första 4 (550000 → 5500 ≥ 5000 → BS)
        self.assertEqual(saft_parser.statement_type_from_code("550000", "DK"), "BS")

    def test_non_digit_is_none(self):
        self.assertIsNone(saft_parser.statement_type_from_code("", "NO"))
        self.assertIsNone(saft_parser.statement_type_from_code("ABC", "NO"))


class OrgnrNormalization(unittest.TestCase):
    def test_swedish(self):
        self.assertEqual(saft_parser.normalize_orgnr("556071-2340"), "5560712340")

    def test_norwegian_mva(self):
        self.assertEqual(saft_parser.normalize_orgnr("NO818488262MVA"), "818488262")

    def test_danish_spaces(self):
        self.assertEqual(saft_parser.normalize_orgnr("29 14 36 25"), "29143625")


class HeaderParsing(unittest.TestCase):
    def setUp(self):
        self.parsed = saft_parser.parse_saft(_write(NO_XML))

    def test_orgnr_from_company_not_sender(self):
        # B6: orgnr ska tas från Company, aldrig AuditFileSender (999888777).
        self.assertEqual(self.parsed["orgnr"], "916059701")

    def test_name_and_currency(self):
        self.assertEqual(self.parsed["name"], "Test Company AS")
        self.assertEqual(self.parsed["currency"], "NOK")

    def test_period_fields(self):
        self.assertEqual(self.parsed["period_end_year"], "2026")
        self.assertEqual(self.parsed["period_end_month"], "4")
        self.assertEqual(self.parsed["selection_end_date"], "2026-04-30")


class AccountParsing(unittest.TestCase):
    def setUp(self):
        self.rows = saft_parser.parse_saft(_write(NO_XML))["accounts"]

    def test_account_tuple_shape_and_sign(self):
        # (code, name, amount=closingDebit-closingCredit, statement_type, idx)
        self.assertEqual(self.rows[0], ("1500", "Kundefordringer", 1000.0, "BS", 1))
        self.assertEqual(self.rows[1], ("3000", "Salgsinntekt", -1000.0, "IS", 2))

    def test_amount_is_float(self):
        # Kontraktsgräns: float (inte Decimal), så orakel-hashar stämmer.
        self.assertIsInstance(self.rows[0][2], float)


class JournalIteration(unittest.TestCase):
    def setUp(self):
        parsed = saft_parser.parse_saft(_write(NO_XML))
        self.path = _write(NO_XML)
        self.lines = list(saft_parser.iter_saft_journal(self.path, parsed["ns"]))

    def test_two_lines_yielded(self):
        self.assertEqual(len(self.lines), 2)

    def test_debit_line(self):
        ln = self.lines[0]
        self.assertEqual(ln["account_code"], "1500")
        self.assertEqual(ln["debit"], 1000.0)
        self.assertEqual(ln["credit"], 0.0)
        self.assertEqual(ln["value_date"], date(2026, 3, 31))
        self.assertEqual(ln["record_id"], "R1")

    def test_credit_line_without_valuedate(self):
        ln = self.lines[1]
        self.assertEqual(ln["credit"], 1000.0)
        self.assertEqual(ln["debit"], 0.0)
        self.assertIsNone(ln["value_date"])


class Periodization(unittest.TestCase):
    """B2: ValueDate (linjenivå) styr period, annars TransactionDate-fallback."""

    def setUp(self):
        parsed = saft_parser.parse_saft(_write(NO_XML))
        self.lines = list(saft_parser.iter_saft_journal(_write(NO_XML), parsed["ns"]))

    def test_valuedate_drives_period(self):
        # Linje 1 har ValueDate 2026-03-31 → period 202603 (inte TransactionDate jan).
        self.assertEqual(saft_parser._journal_period(self.lines[0], "202604"), "202603")

    def test_transactiondate_fallback(self):
        # Linje 2 saknar ValueDate → TransactionDate 2026-01-15 → 202601.
        self.assertEqual(saft_parser._journal_period(self.lines[1], "202604"), "202601")


class PeriodDerivation(unittest.TestCase):
    def setUp(self):
        self.parsed = saft_parser.parse_saft(_write(NO_XML))

    def test_derive_period_from_period_end(self):
        self.assertEqual(saft_parser.derive_period(self.parsed, None), "202604")

    def test_override_wins(self):
        self.assertEqual(saft_parser.derive_period(self.parsed, "202603"), "202603")

    def test_fy_range_from_period_fields(self):
        self.assertEqual(saft_parser.derive_fy_range(self.parsed, "202604"),
                         ("202601", "202604"))


_NS_NO = "urn:StandardAuditFile-Taxation-Financial:NO"


def _no_masterfiles(body: str) -> str:
    """Minimal NO-fil med valfritt MasterFiles-innehåll (för dim-tester)."""
    return (f'<AuditFile xmlns="{_NS_NO}"><Header>'
            f'<Company><RegistrationNumber>916059701</RegistrationNumber>'
            f'<Name>X</Name></Company></Header>'
            f'<MasterFiles>{body}</MasterFiles></AuditFile>')


class AnalysisTypeTable(unittest.TestCase):
    """parse_saft ska läsa MasterFiles/AnalysisTypeTable → out['analysis_types']."""

    def test_reads_type_and_member_descriptions(self):
        parsed = saft_parser.parse_saft(_write(_no_masterfiles(
            '<AnalysisTypeTable>'
            '<AnalysisTypeTableEntry><AnalysisType>DEP</AnalysisType>'
            '<AnalysisTypeDescription>Avdeling</AnalysisTypeDescription>'
            '<AnalysisID>3</AnalysisID>'
            '<AnalysisIDDescription>Montørstab</AnalysisIDDescription>'
            '<Status>Active</Status></AnalysisTypeTableEntry>'
            '</AnalysisTypeTable>')))
        self.assertEqual(parsed["analysis_types"],
                         [("DEP", "Avdeling", "3", "Montørstab")])

    def test_missing_table_yields_empty_list(self):
        parsed = saft_parser.parse_saft(_write(_no_masterfiles("")))
        self.assertEqual(parsed["analysis_types"], [])


class JournalAnalysis(unittest.TestCase):
    """iter_saft_journal ska yielda Analysis-block per linje (0..N)."""

    def _journal(self, line_inner: str) -> Path:
        xml = (f'<AuditFile xmlns="{_NS_NO}"><GeneralLedgerEntries><Journal>'
               f'<JournalID>J1</JournalID><Description>d</Description>'
               f'<Transaction><TransactionID>T1</TransactionID>'
               f'<TransactionDate>2026-04-30</TransactionDate>'
               f'<Line><RecordID>1</RecordID><AccountID>3000</AccountID>'
               f'{line_inner}'
               f'<DebitAmount><Amount>100</Amount></DebitAmount></Line>'
               f'</Transaction></Journal></GeneralLedgerEntries></AuditFile>')
        return _write(xml)

    def test_two_analysis_blocks_yielded(self):
        rows = list(saft_parser.iter_saft_journal(self._journal(
            '<Analysis><AnalysisType>DEP</AnalysisType><AnalysisID>3</AnalysisID></Analysis>'
            '<Analysis><AnalysisType>PRO</AnalysisType><AnalysisID>1</AnalysisID></Analysis>'),
            _NS_NO))
        self.assertEqual(rows[0]["analysis"], [("DEP", "3"), ("PRO", "1")])

    def test_no_analysis_blocks_yields_empty(self):
        rows = list(saft_parser.iter_saft_journal(self._journal(""), _NS_NO))
        self.assertEqual(rows[0]["analysis"], [])


if __name__ == "__main__":
    unittest.main()
