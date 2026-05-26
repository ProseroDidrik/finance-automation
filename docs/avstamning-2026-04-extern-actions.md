# Avstämning 2026-04 — externa actions

**Sammanställd 2026-05-26.** Avstämningen mot Mercur-backup är formellt i mål
(92.7% ok). De 7 listade avvikarna är inte ETL-buggar utan kräver externa
åtgärder eller är accepterade som "förväntat Mercur-config-brus" enligt
[[reference-mercur-config-not-sie]].

Den här filen samlar de externa kontakterna i ett ställe så ingen tappas bort.

## Action-lista

### 1. Bolag 101 Axlås konsol — be Mercur uppdatera konsolkonfig för 2026

**Symtom:** Bolag 101 (konsol över bolag 1 Axlås) har 0 rader i
`backup_from_mercur` för 2026. Konsoliderade siffror kan inte verifieras mot
facit.

**Action:** Kontakta Mercur-supporten och be dem lägga in 101-konsoliderings-
config för räkenskapsåret 2026 i Mercur Utfall-vyn.

**Status om ej svar:** Bolag 101 är 1:1-konsol över bolag 1 — det enda barnet.
Verifiering kan göras indirekt på bolag 1:s siffror. Inte kritiskt.

---

### 2. Bolag 51 Prosero Security Group — be ekonomi om kompletterad SIE

**Symtom:** SIE-filen från bolag 51 saknar löner och räntor (`#VER`/`#TRANS`
ej exporterade för dessa konton). Sammanlagt ~7,4M på 7XXX/8XXX-konton som
inte når warehouse.

**Action:** Kontakta ekonomi-ansvarig för Prosero Security Group och be om:
- Kompletterad SIE 202604 där löne- och räntejournal är inkluderade
- ELLER bekräftelse att löner/räntor bokförs i annat system och inte ska finnas
  i SIE-exporten

**Status om ej svar:** Memory [[sie-ver-hybrid-fallback]] beskriver hybrid-
fallback som tar `#RES` jämnt fördelat för konton som saknar `#VER`. Aktiveras
för 51 om `#RES` har värden. Verifiera via `load_history.message`
(`sie_ver_fallback=N`).

---

### 3. Bolag 229 Zipp Systems (DK) — be om kompletterad INL 202602

**Symtom:** INL-laddningen för 202602 är ofullständig — Mercur har manuella
konteringar som inte återfinns i bolagets INL-export.

**Action:** Kontakta ekonomi-ansvarig för Zipp Systems och be om:
- Kompletterad INL.xlsx för 202602 med manuella konteringar inkluderade
- ELLER bekräftelse att manuella konteringar görs direkt i Mercur (inte i Zipps
  bokföringssystem)

**Status om ej svar:** Mercur-siffran är facit. Diff är dokumenterad och
accepterad.

---

### 4. Bolag 145 Prosero Security OY (FI) — be om Tax jan + ICT mar

**Symtom:** IMP-laddningen saknar:
- Tax-konton för januari 2026
- ICT-kostnader för mars 2026

**Action:** Kontakta Prosero Security OY:s ekonomi och be om:
- IMP.xlsx-komplettering med Tax-konton (TyEL / Veroprosentit / motsvarande)
  för 202601
- ICT-rader för 202603

**Status om ej svar:** Mindre diff (sum=0 båda sidor på rapporterad nivå —
sannolikt klassificerings-grovhet, inte saknad summa).

---

### 5 + 6. Bolag 158 Asker + 189 Lås & Prosjekt — Tripletex-export-konfig

**Symtom:** Två sammanflätade problem från Tripletex:
- **158/189:** SAFT-export bokar hela årets avskrivning i januari
  (`-56 640 = 12 × -4 720` per memory). Mercurs egna NO-parser sprider över FY.
- **189 specifikt:** Tripletex aggregerar 16 underkonton till `3000` vilket vår
  ETL inte kan särskilja. Resulterar i ~3,2 % avvikelse på intäktssidan.
- **Båda:** ClosingBalance > sum(GL-entries) i samma SAFT-fil (känt
  Tripletex-mönster per [[warehouse-semantics]]).

**Action:** Kontakta ekonomi för Asker och Lås & Prosjekt och be dem:
- Granska Tripletex-export-konfigurationen för avskrivningskonton (årlig vs
  månatlig fördelning)
- Granska konto 3000-aggregeringen (16 underkonton som kollapsas)
- Om möjligt: konfigurera Tripletex att exportera periodiserade siffror eller
  inkludera underkonton i SAFT-utdata

**Status om ej svar:** Accepterat brus per warehouse-semantik. ~3 % avvikelse
för dessa två bolag är dokumenterad och inte ETL-bug. Påverkar inte rapporter
mot `fact_balances` direkt — bara mot Mercur-backup.

---

### 7. Bolag 93 Hässleholm — be ekonomi om jan-dubbelbokning + perioderingsfix

**Symtom (utredd på djupet 2026-05-26):** 4 konton avviker mellan bolagets SIE
och Mercur-backupen:

| Konto | Diff (YTD jan-apr) | Mönster |
|---|---:|---|
| 5010 (lokalhyra?) | +58,6k | **Dubbelbokning i januari** — exakt 2× månadshyran 58 620,80 |
| 5615 | +16,5k | **Dubbelbokning i januari** — ungefär 2× |
| 4010 | +133k (≈4,7 %) | Period-flytt mellan feb och mar (~248k flyttat) |
| 5220 | +18,7k | Småposter, blandat mönster |

**ETL är verifierad korrekt 2026-05-26.** `fact_journal_sie` speglar bolagets
SIE-fil exakt; `compare_all_file_vs_db.py` jämför rätt mot Mercur-backupen.
Diff:en kommer från reell bokföringsskillnad — bolaget har:
- Bokat samma januari-belopp två gånger på 5010 och 5615
- Periodiserat 4010-kostnader mellan februari och mars i sin SIE, men Mercur
  fryste månaden på en tidigare snapshot

**Detta är inte samma sak som "Mercur är statisk konfig"** — den hypotesen
från ett tidigare post-mortem var fel. Det är reell källskillnad.

**Action:** Kontakta Prosero-Hässleholms ekonomi och be om:
- Granska om 5010 och 5615 verkligen ska vara dubbelbokade i januari, eller om
  det är felbokning som ska korrigeras
- Bekräfta att 4010-perioderingen mellan feb/mar är medveten (kvartalsfaktura
  som spillde över?)

**Status om ej svar:** Diff är dokumenterad och spårad i `compare_overrides.json`
(klass C, period 202601-202604). Inverkan på koncernrapporter är marginell —
båda källorna har konsekvent jan-apr-summor inom ~5 % för dessa konton.

---

## Bolag som INTE kräver action

(Lista uppdateras när nya fall verifieras. Tidigare nämnda 14 SE-bolag som
"Mercur-config-brus" finns inte som distinkt kategori — den hypotesen är
korrigerad efter 93-utredningen 2026-05-26.)

## Spårning

När ett externt svar kommer in:
1. Notera i `load_history`-meddelande eller annan persistent plats
2. Uppdatera den specifika punkten ovan med datum och resultat
3. Kör om relevant `load_*.py` om nya filer levererats
4. Uppdatera `_uploads/Alla bolag - jamforelse fil vs warehouse.xlsx`
