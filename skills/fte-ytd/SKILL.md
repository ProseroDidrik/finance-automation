---
name: fte-ytd
description: Bygg en YTD-nyckeltal-dashboard för Prosero-koncernen (omsättning, personalkostnad, bruttovinst, FTE, omsättning/anställd, bruttovinst-kr/personal-kr, etc.) per bolag och koncerntotal — med valfri validering mot Mercur Resultaträkning-export. Använd denna skill när användaren ber om "nyckeltal", "YTD-rapport", "dashboard för Eva", "FTE-rapport", "omsättning per anställd", "personalkostnadsutveckling", "jämför warehouse med Mercur", "ladda om dashboarden", "uppdatera ekonomidashboarden" eller liknande för Prosero. Skillen kapslar in describe_schema → YTD-query med korrekt period-semantik → reporting unit-aggregat (sub-bolag absorberas i konsoliderade enheter) → personal från reporting.personnel → valfri facit-validering med status-dots → standalone HTML-dashboard. Triggar även om användaren bara säger "kör om" i samband med tidigare ekonomi-arbete.
---

# fte-ytd — Prosero YTD-nyckeltalsdashboard

## När skill ska användas

Användaren vill ha en YTD-nyckeltal-dashboard för Prosero-koncernens nordiska bolag — typiskt som underlag inför ett ledningsmöte eller styrelsemöte. Vanliga formuleringar: "ta fram nyckeltal", "uppdatera Eva-rapporten", "bygg om dashboarden", "kör om med ny data", "validera mot facit", eller efter ändringar i warehouse där användaren vill se effekten.

Skillen producerar en standalone HTML-fil som öppnas i webbläsaren med:
- KPI-kort för koncerntotal (Sales, Bruttovinst, Personalkost, Personalkost%, Bruttomarginal)
- Sorterbar tabell över alla reporting units med Total Sales, Bruttovinst, Personal, FTE, hires/leavers, Oms/FTE, BV/FTE, BV/PKr
- Drilldown per bolag med kostnadsbreakdown
- Utstickar-lista (top/bottom på olika dimensioner)
- Valfri valideringsflik med facit-jämförelse från Mercur Resultaträkning-uppladdning, inklusive aaro-konto-drilldown

## Workflow

### 1. Anropa describe_schema först — alltid

`finance-warehouse` ändras mellan körningar (nya tabeller, omladdad data, uppdaterad semantik). Skripten i denna skill antar att du läst dagens schema. Hoppar du över detta steg räknar du fel.

### 2. Identifiera vad användaren faktiskt vill

Ställ klargörande frågor via `AskUserQuestion` om de inte är uppenbart:
- Vilka perioder? (default: YTD apr 2026 + YTD apr 2025 + helår 2025 för per-huvud-tal)
- Vilken scope? (default: alla bolag i alla länder)
- Format? (default: HTML-dashboard, standalone, ingen internet-beroende)
- Facit-validering? (om Mercur Resultaträkning-fil bifogats, kör jämförelsen)

### 3. Dra rådata

Kör SQL-queries i `scripts/sql_queries.py` — eller kopiera direkt från `references/sql_patterns.md`. Detta ger:
- `ytd_topgroup_allkinds.json` — YTD per (bolag, period, top_group) för 202504, 202604, 202512. För full_year_only-bolag fungerar 202512 som helårsproxy (de saknar 202504).
- `full_year_only_cids` — kör `FULL_YEAR_ONLY_DETECT_QUERY` (dynamisk detektion av bolag med bara helårs-SAFT 2025). Skicka in som sista argument till `build_dashboard_data`.
- `personnel.json` — FTE och brutto-rörelse per bolag och datum
- `wh_aaro_202604.json` — Aaro-rollup för facit-validering
- `dim_company.json` — Bolagsregister med parent_id för RU-bygge

**Kritiskt:** Använd korrekt period-semantik. `SIE_PSALDO` och `IMP` är `monthly` — måste summeras jan..period. `SIE`, `SIE_VER`, `SAFT` är `ytd` — ta vid period. Detta är den vanligaste fällan. Se `references/sql_patterns.md`.

### 4. Bygg reporting units

Aggregera per reporting unit, inte per cid:
- `kind='consolidated'` → reporting unit, absorberar alla subs med `parent_id = cid`
- `kind='standalone'` → reporting unit för sig
- `kind='sub'` eller `'decommissioned_sub'` utan consolidated parent → "orphan_sub" reporting unit

Detta matchar hur Mercur visar koncernen och undviker dubbelräkning. Sub-bolagen ska inte synas separat i toppnivåtabellen — bara den konsoliderade enheten.

### 5. Tecken-normalisering

Konvertera till positiva magnituder (intäkt positiv, kostnad positiv) genom `abs()` per (cid, period, top_group). Detta är robust mot tecken-konventionsavvikelser per bolag (t.ex. Actas DK som hade SIE-konvention trots att DK normalt är Mercur-konvention).

Den vanliga regeln "SE/NO/CA = SIE-konvention, DK/FI/DE = Mercur-konvention" stämmer inte för alla bolag. Använd `abs()` istället för att gissa per land.

### 6. Bygg HTML

Använd `scripts/build_html.py` som tar in JSON-payloads och renderar en standalone HTML med inline JS+CSS. Inga externa dependencies. Bädda all data i `<script>`-blocket som `const DATA = ...; const VALIDATION = ...; const AARO_DATA = ...`.

**Viktigt:** validera JavaScript-syntax med `node --check` innan leverans. Template literals med `${...}` är känsliga — ett enstaka `}` på fel plats stoppar hela renderingen.

### 7. Facit-validering (om relevant)

Om användaren bifogat en `Resultaträkning*.xlsx`-fil från Mercur:
- `Resultaträkning Bolag.xlsx`: bolagslista med indenterade rader (cid + namn, indenterade subs under consolidated parent)
- `Resultaträkning (20).xlsx` eller liknande: top_group-nivå per bolag (Total Försäljning, Personalkostnader, etc.)
- `Resultaträkning (21).xlsx` eller liknande: aaro-konto-nivå per bolag

Använd `scripts/parse_mercur.py` och `scripts/validate_facit.py`. Mappnings-tabellen (Mercur-namn → warehouse company_id) finns i `references/mercur_mapping.md`.

### 8. Status-dots

Visa en grön/gul/röd cirkel bredvid varje bolagsnamn i dashboarden:
- 🟢 Grön: |diff| < 1% mot facit (nästintill exakt)
- 🟡 Gul: 1-5% diff
- 🔴 Röd: > 5% diff
- ⚪ Grå: ej i Mercur-rapporten
- 🔸 Helår-proxy (violet): bolaget har endast helårs-SAFT för 2025 (ingen månadsvis data). Jämför mot Mercurs helår, inte YTD apr. Se pitfall #12.

### 9. Leverera

Spara HTML till user-mapp (`/sessions/.../mnt/<user-folder>/`) och anropa `present_files`. Standardstorlek ~500 KB inklusive embedded data.

## Kända fallgropar — läs alltid

Se `references/known_pitfalls.md` för utförliga exempel. Snabblista:

1. **`SIE_PSALDO` är monthly**, inte ytd. Måste summeras jan..period. Klassisk error som ger ~75% partial data.
2. **`fact_journal_saft` syntetiseras INTE för 2025** (borttaget i v1.4 — bara ~6% inläst, fabricerade siffror). Bolag utan månadsvis SAFT 2025 → helårsproxy, se #9.
3. **`reporting.personnel`** istället för `public.fact_personnel` (PII-restriction). FTE = `SUM(COALESCE(employment_pct, 1.0))`, inte `COUNT(*)`.
4. **CENTR-bolag** (Elimineringsbolag, Prosero Security AS/AB/GmbH/Holding) ska EXKLUDERAS från utstickar-toppar — de är centrala stödbolag som ska visas men inte tävla mot operativa bolag.
5. **Mappnings-fel: "Ålesund"** i Mercur är cid 77 (Låsservice Ålesund), inte cid 80 (Lockit). Se hela tabellen i `references/mercur_mapping.md`.
6. **JS template literal-fällan**: `${... + ' pe})` — `}` inuti texten stänger `${}` för tidigt. Skriv `${... + ' pe'})` (med stängande quote).
7. **Output-storlek**: query_sql trunkerar till 50 rader vid större output. Använd `json_agg(row_to_json(t))::text` för att samla allt i en payload, eller acceptera att resultat sparas till fil.
8. **journal_saft-2025-syntes är BORTTAGEN (v1.4)** (bara ~6% inläst → fabricerade siffror). Syntetisera inte Total Sales ur journalen. Se pitfall #11.
9. **~35 bolag har bara helårs-SAFT för 2025** (mest NO), detekteras dynamiskt via `FULL_YEAR_ONLY_DETECT_QUERY` (v1.5; var hårdkodad i v1.4) och skickas in som `full_year_only_cids`. De flaggas `FULL_YEAR_PROXY_2025`; financial-YoY mot 202504 nullas (jämför 202512-helår mot Mercurs HELÅRSSIFFRA). FTE-delta behålls bara om apr-2025-snapshot finns. **v1.6: bolag med `SAFT_VER`-syntes (t.ex. Actas 81) exkluderas** — de har en riktig interim-baslinje ur journalen och får normal YoY i st. `SAFT_VER` är inkopplat i `base_pick` (under SAFT). Se pitfall #12.
10. **CENTR-valuta är fixad i prod (2026-06-01)** via `db.py COMPANY_CURRENCY_OVERRIDE` (50/51/53 SEK, 52 NOK, 54 DKK). Ingen currency-override behövs längre i skillen — `dim_company.currency` är korrekt. Se pitfall #9 i known_pitfalls.
11. **Delade koder (P_30 m.fl., '_', BUDG) droppades tyst — FIXAT v1.7.** MAN/IMP_ADJ-justeringar bokas ofta på bolagsagnostiska noder (`company_id=NULL`); den normala `walk` (`company_id IS NOT NULL`) missar dem och INNER-joinen kastade dem. `YTD_TOPGROUP_QUERY` har nu en `pwalk`-gren + `(ag.company_id = y.company_id OR ag.company_id IS NULL)`-join (speglar `report_pnl.sql:177`). Konkret: cid 160 Total Sales 202504 83,09→86,09 MSEK.

## Filstruktur

```
fte-ytd/
├── SKILL.md (denna fil)
├── references/
│   ├── sql_patterns.md     — Korrekt YTD-CTE med period_type-medvetenhet
│   ├── mercur_mapping.md   — Bolagsnamn-mappning Mercur → warehouse cid
│   ├── known_pitfalls.md   — Detaljerade fallgropar med exempel
│   └── dashboard_layout.md — Vad dashboard ska innehålla, layout-principer
└── scripts/
    ├── sql_queries.py       — Återanvändbara SQL-templates
    ├── build_ru_aggregat.py — Bygg reporting units från ytd-data
    ├── parse_mercur.py      — Parsa Resultaträkning-xlsx-filer
    ├── validate_facit.py    — Bygg validation_final.json
    └── build_html.py        — Rendera HTML-dashboarden
```

## Standardvärden

- **Senaste period** (YTD anchor): hämta från senaste `period` med scenario='A' i fact_balances (typiskt 202604 idag).
- **Jämförelseperiod**: samma månad föregående år (typiskt 202504).
- **Helår-anchor**: senaste decembern med data (typiskt 202512) — för per-huvud-tal (LTM-proxy).
- **FX-rates**: dim_exchange_rate avg per månad. För 202604 (saknas) använd snitt av 202601-202603.
- **Output-folder**: läs `Get testfiles` eller motsvarande user-mounted folder. Spara HTML där.

## Versionshistorik

- **v1.7** (2026-06-01): Delade/bolagsagnostiska koder (P_30 …, `_`, `BUDG`; `company_id=NULL` i `dim_account_map`) mappas nu till top_group. Tidigare seedade `walk` bara `company_id IS NOT NULL` → MAN/IMP_ADJ bokade på delade koder droppades tyst av INNER-joinen i `YTD_TOPGROUP_QUERY`. Fix: `pwalk`-CTE + `acc_topgroup`-UNION (company_id=NULL) + slutjoin `(ag.company_id = y.company_id OR ag.company_id IS NULL)` — speglar `report_pnl.sql:177`. Verifierat mot prod: cid 160 (Passera & L&S KBA) Total Sales 202504 83,09→86,09 MSEK (stänger Mercur-gapet), helår 202512 oförändrat (P_30/P_35 nettar 0), koncerntotal 202604 1591→1594 MSEK (+0,2%, inom grind). Samma fix i `dashboards/ytd_nyckeltal/queries.py` + `aaro.py` + `references/sql_patterns.md`. Pitfall #11.
- **v1.6** (2026-06-01): `SAFT_VER` inkopplat som källa i `base_pick` (under SAFT, ytd-gren) så annual-only-bolag med journal-syntetiserade interim-saldon (synthesize_saft_ver.py) får riktig interim-YTD. `FULL_YEAR_ONLY_DETECT_QUERY` exkluderar nu SAFT_VER-täckta bolag → de tappar proxy-flaggan och får normal YoY. Verifierat mot prod: Actas (81) 2025-apr 0→75,23 MSEK, YoY +8,3% mot 81,46; full_year_only 36→35, proxy-RU:er 32→31; koncerntotal/RU-antal oförändrade.
- **v1.5** (2026-06-01): Dynamisk detektion av full_year_only-mängden via `FULL_YEAR_ONLY_DETECT_QUERY` (ersätter v1.4:s hårdkodade 36-cid-lista — slipper underhåll vid SAFT-omladdning). Idé från en parallell v1.3.1; resten av v1.3.1 (currency-override, oguardad fte_delta) togs INTE in — override är obsolet (prod fixad) och fte_delta-guarden behölls. Ny signatur: `build_dashboard_data(ytd, companies, personnel, fx, full_year_only_cids)`.
- **v1.4** (2026-06-01): Faktiskt IMPLEMENTERAT det v1.3 bara dokumenterade. Tog bort `NO_YTD_2025_SYNTH_QUERY` och journal-syntes-mergen i `build_ru_aggregat` (fabricerade NO-2025-siffror). Hårdkodad `FULL_YEAR_ONLY_2025` (36 cids) → flaggar `FULL_YEAR_PROXY_2025`, nullar financial-2025-delta (behåller FTE-delta), använder befintlig 202512-SAFT som helårsproxy. CENTR currency-override behövs ej längre — `dim_company.currency` rättad i prod (db.py). Verifierat mot prod 2026-06-01.
- **v1.3** (2026-06-01): Avsåg sluta fake-syntetisera 2025 ur journal_saft + helårsproxy, men ändringen nådde bara dokumentationen — koden syntetiserade fortf. (rättat i v1.4).
- **v1.2** (2026-05-30): Symmetric MAN-omklassificeringar identifierade. Col 139 från Mercur används som 2025-facit. CENTR currency override för cid 50/51/52/53/54.
- **v1.1** (2026-05-29): SIE_PSALDO/IMP behandlas som monthly. CENTR-filter. Aaro-drilldown för båda år.
- **v1.0** (2026-05-28): Första versionen. RU-aggregering, facit-validering, dashboard med status-dots.

## Iteration

Användaren itererar ofta — efter en körning vill hen typiskt:
- Lägga till ny info (status-dots, drilldown, ny kolumn)
- Bygga om med uppdaterad warehouse-data (kör om describe_schema + queries)
- Ändra mappningar eller filter
- Validera mot ny facit-upload

För "kör om"-iterationer: börja alltid med describe_schema igen och kör hela pipelinen — du kan inte anta att datan är samma som förra körningen.
