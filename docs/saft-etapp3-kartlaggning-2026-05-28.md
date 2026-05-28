# Etapp 3 — SAF-T kartläggning + avvikelselista

Referens: **Norwegian_SAF-T_Financial_Schema v1.30** (targetNamespace
`urn:StandardAuditFile-Taxation-Financial:NO`) som auktoritativt XSD.
DK-fynd hävdas **via inspektion** (inget separat DK-XSD hämtat) — DK-filerna
(ns `:DK`, AuditFileVersion 1.0) använder samma OECD-elementvokabulär.

xsdata 26.2 genererade 30 main + 67 inner-klasser ur v1.30-XSD:t. De
genererade klasserna **parsar en riktig 1.30-fil (009 Beslag-Consult) felfritt**
— alltså användbara som parse-bas i Etapp 4.

## Datagrundlag (202604, riktiga filer)

| | NO | DK |
|---|---|---|
| Antal | 36 | 2 |
| AuditFileVersion | **33× 1.30, 3× 1.20** | 2× 1.0 |
| Namespace | `...:NO` | `...:DK` |

---

## A. Kartläggning — nuvarande SAF-T-parsningsyta

### Produktionsparsning (3 vägar, i scope för avvikelser)

1. **`load_saft.py` — `parse_saft()` + `iter_saft_journal()`** (huvudloader)
   - `xml.etree.ElementTree.iterparse` (streamande). Namespace detekteras ur
     rotelementet (`_detect_namespace`), alla sökningar prefixas `{ns}`.
   - `parse_saft`: läser Header (Company/RegistrationNumber, Name,
     DefaultCurrencyCode, SelectionCriteria) + alla `Account` i MasterFiles
     (AccountID, AccountDescription, ClosingDebit/CreditBalance). Stoppar vid
     `GeneralLedgerEntries`.
   - `iter_saft_journal`: streamar Journal→Transaction→Line och yield:ar
     journal_id/desc, transaction_id/date/desc, **value_date**, line_no,
     record_id, account_code, line_desc, debit, credit.
   - Mappar → `fact_balances` (source_kind SAFT, period_type ytd) +
     `fact_journal_saft`.

2. **`process_norway.py` — `parse_saft_header()` + `find_company_registration()`**
   (filnamnsdöpning, ej DB)
   - `ET.fromstring` (hela filen i minnet) + `root.iter()` namespace-agnostiskt
     via `strip_ns`. Läser SoftwareID, RegistrationNumber (scopat till Company),
     PeriodStartYear/PeriodEnd resp. SelectionStartDate/EndDate.

3. **`load_history_sie_saft.py` — `_quick_orgnr_saft()`** (historik-orgnr-lookup)
   - `iterparse`, returnerar **första** `RegistrationNumber` i trädet; återanvänder
     i övrigt `load_saft.load_file` + `load_saft.build_orgnr_lookup`.

### Diagnostik-/engångsskript (utanför avvikelse-scope, listade för fullständighet)

- `scripts/inspect_saft_valuedate.py` — rå XML-dump av datumtaggar (ValueDate-utredningen).
- `scripts/check_saft_txn_dates.py`, `scripts/verify_saft_reload.py`,
  `scripts/check_saft_journal_dups.py`, `scripts/analyze_saft_dup_classes.py`
  — Postgres-frågor / regressionskontroller, ingen produktionsparsning.

---

## B. Avvikelselista (mot v1.30-XSD + genererade klasser)

Allvarlighetsgrad: **[H]** påverkar korrekthet/robusthet i Etapp 4-refaktorn,
**[M]** datakomplett­het, **[L]** kosmetik/latent.

### B1. Versionshantering — [H], den centrala Etapp 4-risken
- **3 av 36 NO-filer är v1.20** och använder `Account/StandardAccountID` (gamla
  modellen) i stället för v1.30:s `GroupingCategory`/`GroupingCode`. De
  genererade **1.30-klasserna kraschar på 1.20-filer** (verifierat: 158 Asker,
  `ParserError: Unknown property Account:StandardAccountID`).
- DK-filerna är **ver 1.0** i `:DK`-namespace.
- Konsekvens: en strikt "parsa-allt-via-1.30-dataclasses"-design bryter mot
  3 NO + 2 DK live-filer. Etapp 4 måste hantera flera versioner/namespace
  (generera per version, eller läsa lenient/bara de fält vi behöver, eller
  versions-routa). Dagens loader överlever just för att den bara plockar de
  fält den bryr sig om.

**Empiriskt test av lenient-parsning (`fail_on_unknown_properties=False`) mot 1.30:**

| Fil | Lenient 1.30 | Orsak |
|-----|--------------|-------|
| NO 1.30 (33 st) | ✅ OK (676 konton, journal) | matchar |
| NO 1.20 (3 st) | ❌ `Account.__init__() missing 2 required` | 1.30 kräver `GroupingCategory`/`GroupingCode`; 1.20 har `StandardAccountID` |
| DK 1.0 (2 st) | ❌ `AuditFile.__init__() missing 'header'` | xsdata matchar på kvalificerat namn — `:DK`-element matchar inte `:NO`-klasserna → tomt träd |

Lenient tolererar *okända* element men **inte saknade obligatoriska**, och löser
inte namespace-krocken. Ren (a)/(b)/(c) räcker alltså inte — beslutet blev en
hybrid (se nedan).

### B2. Periodisering — `Transaction.Period`/`PeriodYear` oanvända — [M]
- XSD: `Transaction.Period` (int) + `PeriodYear` (int, 1970–2100) är
  **obligatoriska** och korrekt ifyllda (009: månad 1–4; 158: spridda).
- Loadern läser dem inte alls — den periodiserar på `ValueDate` (linjenivå) →
  `TransactionDate` (fallback).
- **Viktigt (verifierat, korrigerar tidig hypotes):** `Period`/`PeriodYear` är
  per *transaktion* och skulle klumpa fler­månads-verifikat exakt som
  `TransactionDate`. På 158 (Tripletex) har en avskrivnings-/årsverifikation
  **en** TransactionDate men 12 linjer med olika `ValueDate` (5900/5900 linjer
  har ValueDate, spridda jan–dec). Alltså: **ValueDate är det enda
  linjenivå-korrekta fältet — b711832-fixen står sig.** Period/PeriodYear bör
  inte ersätta ValueDate, men duger som **valideringskorskontroll** ("stämmer
  ValueDate-härledd period mot transaktionens deklarerade Period?").

### B3. Ignorerade element som finns i datat — [M]
Per linje (XSD `Line`), uppmätt på riktiga filer:
- **`Analysis`** (dimensioner: kostnadsställe/projekt) — finns på **11204/11451
  linjer (98%)** i 009, dubbla Analysis-block i DK 081. Helt ignorerat.
- **`TaxInformation`** — 3026 linjer i 009. Ignorerat.
- **`CurrencyCode`/`CurrencyAmount`/`ExchangeRate`** (på `AmountStructure`) —
  117 linjer med främmande valuta i 009. Loadern läser bara `Amount`
  (defaultvaluta) → korrekt belopp, men valuta-detaljen tappas.
- `SourceDocumentID`, `ReferenceNumber`, `CID`, `DueDate`, `Quantity`,
  `CrossReference`, `SystemEntryTime`, `OwnerID` — ingen läses (kan vara OK,
  men bör enumereras så du kan välja).
- Per transaktion: `VoucherType`, `VoucherDescription`, `TransactionType`,
  `SystemEntryDate`/`GLPostingDate` (de två sista **obligatoriska** i XSD),
  `BatchID`, `SystemID` — oanvända.

### B4. Kontoklassificering — heuristik i st f filens grouping-koder — [M]
- XSD: `Account.GroupingCategory` + `GroupingCode` är obligatoriska och ifyllda
  (009: `balanseverdiForAnleggsmiddel`/`1000`). DK 1.0 har dem också.
- Loadern ignorerar dem och klassar IS/BS via **första siffran** (NO: 1/2=BS,
  3–9=IS; DK: 4-siffrigt prefix ≤4999=IS). Funkar för standardkontoplan men
  filens egen formella klassificering är mer auktoritativ (och finns i
  repo:ts kodlistor "Grouping Category Code").

### B5. Saldofält — opening-balanser oanvända — [L]
- `Account.OpeningDebitBalance`/`OpeningCreditBalance` läses aldrig. OK för
  YTD-periodslutsfiler, men gör loadern oförmögen att hantera delperiods-filer
  och blockerar saldokontroll (öppning+rörelse=stängning).

### B6. Namespace- & element-sökningsstrategi — 3 implementationer, 2 mönster — [L]
- `load_saft.parse_saft` och `process_norway.find_company_registration` är
  **korrekt scopade** (RegistrationNumber hämtas inuti `Company`).
- `process_norway.find_elem_text` är **namespace-blind whole-tree first-match**.
  Funkar bara för att SoftwareID/PeriodStart*/Selection*Date är unika till en
  plats; skört men ej aktiv bug.
- `load_history._quick_orgnr_saft` tar **första** `RegistrationNumber` i hela
  trädet. Header har både `Company` och `AuditFileSender` (båda
  `CompanyStructure` med RegistrationNumber). Empiriskt: AuditFileSender finns i
  7/36 filer, 2 med eget orgnr — men Company kommer alltid först och har alltid
  orgnr, så **0 filer ger fel svar idag**. Latent skörhet, inte aktiv bug.
  Etapp 4 bör ändå scopa alla tre till `Header/Company`.

### B7. Datatyp/kardinalitet — [L]
- `DebitAmount`/`CreditAmount` är **båda optional** i XSD (en linje har högst
  en). Loaderns `debit - credit` med 0.0-fallback är därför korrekt.
- Belopp har `fraction_digits=2` (XSD → `Decimal`); loadern använder `float`
  via `_amount`. Funktionellt OK, men `Decimal` vore exaktare för pengar.
- Datum: XSD `XmlDate`; loadern slicar ISO-strängar (`_parse_iso_date`,
  `_yyyymm_from_iso`). Funkar, men sårbart för icke-ISO-format.
- `Company/RegistrationNumber` är **optional** i XSD (bara `Name` obligatoriskt)
  — loaderns hårda ERROR vid saknat orgnr är *strängare* än schemat (medvetet,
  rättfärdigar FILENAME_OVERRIDES för Actas).

---

## C. Vad loadern redan gör rätt (så refaktorn inte regredierar)
- Namespace-detektion ur roten + `{ns}`-prefix i huvudloadern.
- Period-härledning ur `SelectionCriteria` (PeriodEndYear+PeriodEnd → SelectionEndDate).
- Orgnr scopat till `Company` i huvudloadern.
- Journal idempotent per **(company_id, period)**, inte per source_file.
- **ValueDate→TransactionDate-periodisering** (b711832) — XSD-semantiskt korrekt:
  ValueDate "rapporteras bara när linjen avviker från TransactionDate".

---

---

## D. Beslutad Etapp 4-scope (klartecken 2026-05-28)

Hybrid — inte en enda klassuppsättning för allt, eftersom lenient-testet (B1)
bevisar att 1.30-klasser faller på både 1.20 och DK:

1. **xsdata bara för Norge.** Patcha `grouping_category`/`grouping_code` till
   optional i de genererade klasserna → en 1.30-uppsättning täcker NO 1.20+1.30
   lenient (vi läser ändå inte grouping-koderna). Verifiera att 1.20 ger
   identiska fältvärden som dagens loader.
2. **DK kvar på nuvarande iterparse-loader** (namespace-krock, 2 filer, fungerar
   redan). Ev. separat uppföljning med DK-XSD.
3. **Journalen streamas per `<Transaction>`** (iterparse → xsdata typar varje
   subträd), aldrig helfil — Actas är 280 MB och COPY-optimeringen bygger på
   streaming. Bara Header + MasterFiles/Accounts helparsas med xsdata.
4. **`xmllint --schema`-grinden version/namespace-routad** — 1.30-schemat
   underkänner 1.20- och DK-filerna.

## Status: KARTLÄGGNING + AVVIKELSELISTA KLAR. Etapp 4 klartecken givet (scope D).
Klon + genererade klasser ligger i `dev/_scratch/` (utanför worktree:t).
