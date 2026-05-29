# SAF-T dimension-konsistenskoll mot prod — 2026-05-29

Kör: `scripts/check_analysis_journal_consistency.py` (read-only, mcp_readonly).
Jämför `fact_saft_analysis` mot `fact_journal_saft` per (bolag, period).
**Endast diagnos — ingen prod-skrivning gjord.**

## Finding A — bolag 104 (SSP): föräldralös 2022-analys (GRIND 1)

Enda (bolag, period) med analys men HELT utan journal: 104, hela 2022, 22 156 rader.

- 104:s journal börjar 202301, balans (SAFT) 202312.
- 2022-analysen kommer från `_history/2022/inl SSP 20230115092019_1_1.xml`.
- 104:s 2022-fil laddades **aldrig** in i journal/balans historiskt (sannolikt
  rejekterad av full-loadern), men den lättare analys-backfillen extraherade
  dimensioner ur den ändå → föräldralös analys.

## Finding B — per-period-DELETE-clobber drabbar BÅDA loaders

Rotorsak: både `load_file` (produktion) och `backfill_file_analysis` raderar
per (bolag, period) före insert. Efter ValueDate-periodisering (b711832-fixen)
spänner EN fil över många perioder, och en fil med strö-ValueDate-rader i en
annan fils månad raderar den filens data och lägger bara in sina egna rader.
Sista skrivaren vinner.

**B1 — JOURNAL clobbrad av produktionsladdningen (FÖRBEFINTLIG, inte denna
feature).** `extracted/202604/...`-filen för bolag 9 har strö-ValueDate-rader i
dussintals gamla månader. När 202604 laddades raderade den (9, 202203) m.fl. och
la in en handfull rader:

| period | journal | källa |
|--------|--------:|-------|
| 202202 |  2966 | _history/2022 (intakt) |
| 202203 |     8 | extracted/202604 (clobbrad) |
| 202204 |  2622 | _history/2022 (intakt) |
| 202501 |     8 | extracted/202604 (clobbrad) |
| 202502 |  2786 | _history/2025 (intakt) |

Min backfill rörde ALDRIG journalen (verifierat oförändrad) — detta är ett
`load_file`-beteende som förelåg före dimensionsarbetet (sannolikt 2026-05-28-
reloaden). Konsistenskollen **avslöjade** det.

**B2 — ANALYS clobbrad av backfillen.** Bekräftat entydigt på bolag 9: inom
samma era (2022-filen) varvas hög och nära-noll analys beroende på vilken fil
som skrev månaden sist:

| period | journal | analys | analyskälla |
|--------|--------:|-------:|-------------|
| 202204 |  2622 | 7630 | 2022-filen (intakt) |
| 202205 |  3032 |   16 | 2023-filen (strö — clobbrad) |
| 202207 |  2051 | 6020 | 2022-filen (intakt) |

Erratiskt INOM en era = clobber. Bekräftat bolag **9**. Misstänkta som ännu
behöver granneperiod-koll: **8, 176** (200/16 visade sig vara icke-bugg, se nedan).

**Blind fläck i kollen:** där BÅDE journal och analys clobbrats av samma fil
(t.ex. 9/202203: J=8, A=24) stämmer analys ⊆ journal och passerar tyst. Bara en
journal-vs-`fact_balances` (YTD-delta) koll fångar dem.

## INTE buggar (heuristikens falska positiva)

`analys < journal*0.2` flaggar tre legitima mönster:

- **Noll-dimensionsbolag:** 19, 52, 157, 171, 205 — analys=0 i ALLA perioder,
  filerna saknar Analysis-block. Korrekt.
- **Källexport som slutade emitta dimensioner 2025+:** bolag **16 och 200**
  (verifierat). Ren årsbrytning: analys ≈ journal 2022-2024, sedan uniformt
  nära-noll från 2025 (en fil/period, ingen överlappning). Filerna saknar bara
  Analysis från 2025. INTE clobber, INTE vår bug — datan finns inte i filen.
- **Partiell täckning:** bolag 204 — konstant ~15% analys/journal i ALLA
  perioder (bara ~15% av journallinjerna bär Analysis). Korrekt.

## Designspänning i en ev. fix

Naiv fix = DELETE per (bolag, period, **source_file**) så filer samexisterar.
MEN: om en SAF-T-fil re-exporterar full historik blir unionen
**dubbelräkning**, inte komplettering. Nuvarande per-period-DELETE förhindrar
det (en fil äger perioden) men clobbrar när filer är oense om ägandet. Rätt
semantik kräver att avgöra om strö-raderna är dubletter eller äkta korrigeringar
— ett beslut, inte en självklar patch.

## Rekommendation (användarbeslut krävs)

1. **104:** testa om 2022-filen laddar rent i journal. Ja → ladda (fyller gapet,
   mest komplett). Nej → radera den föräldralösa analysen.
2. **Clobber (B1+B2):** logga som eget spår. Fixa DELETE-semantiken i koden
   (billigt, hindrar återfall) men **skjut upp** den stora prod-omladdningen av
   drabbade SAF-T-bolag (journal+analys) tills (a) drill-down i förhistoriska
   månader faktiskt behövs och (b) ett B1ms-säkert/uppskalat fönster finns.
   Rapporter använder `fact_balances` (YTD, orörd), så rapportsiffror påverkas inte.
3. **Källexport-tappet (16/200/…):** ingen åtgärd i vår kod — notera att dessa
   bolags dimensioner saknas i källan från 2025.
4. Lägg `scripts/check_analysis_journal_consistency.py` + en journal-vs-balans-
   delta-koll som återkommande grindar.
