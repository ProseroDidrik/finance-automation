# Avstämning 2026-04 — externa actions

**Sammanställd 2026-05-26. Senast uppdaterad 2026-05-27.** Avstämningen mot
Mercur-backup är formellt i mål (**92.9% ok** efter att 8 Prosero CENTR/CA-bolag
laddats in 2026-05-26/27). 0 ETL-avvik, 0 saknas_i_db. De återstående
extern_action-cellerna är inte ETL-buggar utan kräver externa åtgärder eller är
accepterade som "förväntat Mercur-config-brus" enligt
[[reference-mercur-config-not-sie]].

Den här filen samlar de externa kontakterna i ett ställe så ingen tappas bort.

## Status 2026-05-27 efter compare-körning

| Klass | Antal celler | % | Förändring sedan 2026-05-26 |
|---|---:|---:|---|
| ok | 5 476 | 92.9% | +0.2 p.p. (CENTR/CA-laddningar) |
| periodiseringsbrus | 329 | 5.6% | oförändrat |
| mercur_brus | 14 | 0.2% | oförändrat |
| extern_action | 73 | 1.2% | oförändrat |
| **avvik (ETL-bug)** | **0** | **0.0%** | bibehållet ✅ |
| **saknas_i_db** | **0** | **0.0%** | bibehållet ✅ |

**5 bolag i extern_action_pending:** 93, 158, 189, 229, 145.
**77 bolag helt rena** (inklusive nyladdade 49/50/51/52/53/54/187 och 162 idag).

## Action-lista

### 1. Bolag 101 Axlås konsol — be Mercur uppdatera konsolkonfig för 2026

**Symtom:** Bolag 101 (konsol över bolag 1 Axlås) har 0 rader i
`backup_from_mercur` för 2026. Konsoliderade siffror kan inte verifieras mot
facit.

**Action:** Kontakta Mercur-supporten och be dem lägga in 101-konsoliderings-
config för räkenskapsåret 2026 i Mercur Utfall-vyn.

**Status 2026-05-27:** Inte i `compare`-scope — listad som konsoliderat-bolag
utan egen källdata (skippas av compare-skriptet). 101 är 1:1-konsol över bolag 1
(efter att Begelås 20 dekommissionerades ~2021). Verifiering görs indirekt på
bolag 1:s siffror, som är "ren". Inte kritiskt. Se [[project_axlas_consol]].

---

### 2. ~~Bolag 51 Prosero Security Group~~ — KLAR 2026-05-26

**Symtom (ursprungligen):** SIE-filen från bolag 51 saknade löner och räntor —
`#VER`/`#TRANS` ej exporterade för dessa konton. Sammanlagt ~7,4M på 7XXX/8XXX-
konton som inte nådde warehouse.

**Fix 2026-05-26:** Bolag 51 levererade en ny Fortnox-SIE 8 maj med
löner inkluderade (`ProseroSecurityGroupAB20260508_162116.si`). Laddades in via
`load_sie.py --period 202604 --override 51` → 178 rader SIE/SIE_PSALDO,
1 093 vouchers inkl. +2,49 MSEK ny april-löner. Bolag 51 är nu i "ren"-listan i
compare-resultatet.

**Memory:** [[project_202604_prosero_load]], [[reference_sie_ver_hybrid_fallback]].

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

### 4. Bolag 145 Prosero Security OY (FI) — Tax + INTEX_ICT_CASHPOOL kvarstår

**Status 2026-05-27 efter compare-körning:** 2 öppna kontoklasser i YTD jan-apr:

| Kontoklass | Mercur YTD | DB YTD | Diff | % |
|---|---:|---:|---:|---:|
| INTEX_ICT_CASHPOOL | -184 240,82 | -173 992,53 | -10 248,29 | 5,6% |
| Tax | -341 335,84 | -256 001,88 | **-85 333,96** | 25,0% |

**Tax-diff = exakt -85 333,96** = en månads "Advance tax" (konto 9900). DB har
3 månaders Advance tax, Mercur har 4. Frågan: vilken månad saknas i Postgres,
eller dubbelräknar Mercur en månad?

**INTEX_ICT_CASHPOOL-diff = -10 248,29:** DB har bara konto 9460 (Interest
expenses, credit institutions loans = -173 992,53). Mercur har antingen ytterligare
ränte-konton mappade eller annan kontoklass-definition.

**Fix idag 2026-05-27 (IS/BS-klassning):** De 37 IMP-raderna för 202604 hade
`statement_type = NULL` eftersom `process_finland.read_income_only_xlsx` läser
col B (april-månad) ur 145s IS-fil istället för col C (YTD jan-april) — det gav
sum=365k WARN vid omkörning, så vi byggde istället om INL.xlsx från Postgres-
data + första-siffra-klassning via `scripts/rebuild_145_inl_from_postgres.py`
och laddade om med `--override 145`. Resultat: 29 IS + 8 BS, klassningsfel 0.

**Action (kvarstår):** Kontakta Prosero Security OY:s ekonomi och be om:
- Tax-månadsbreakdown: är 341k YTD eller 256k YTD korrekt för 2026-04?
- Vilka räntekonton ska ingå i INTEX_ICT_CASHPOOL?

**Öppen kodfråga (inte blockerare):** `process_finland.run_145` bör fixas att
läsa col C (YTD) istället för col B för 202604+ för robust re-loading. Idag
matematiskt korrekt men inte reproducerbart från RAW-filerna.

**Memory:** [[project_202604_prosero_load]] (sektion 145), nytt: dagens
classification-rebuild.

---

### 5 + 6. Tripletex-bolag (~19 NO-bolag) — SAFT-export-konfig

**Symtom (utredd 2026-05-27 med journal-baserad jämförelse):**
Tripletex SAFT-export visar **all-i-januari-mönstret** för konto 6010 Avskrivning:

```
Bolag 16 / konto 6010:
  per      Mercur   DB(SAFT)    diff
  202601   -3,275   -13,100     9,825   ← DB har 4x i jan
  202602   -3,275        0     -3,275
  202603   -3,275        0     -3,275
  202604   -3,275        0     -3,275
```

Mercurs egna NO-parser sprider månadsvis; vår SAFT-laddning behåller original-
distributionen som har allt i januari (= 12 × månadsavskrivning).

**Drabbade:** ~19 NO-bolag har samma 6010-mönster (inte bara 158/189). Bolag 9
Beslag-Consult (Visma, inte Tripletex) stämmer 100% → bekräftar att problemet
är Tripletex-exportlogik, inte vår ETL.

Andra Tripletex-relaterade konton (mindre tydligt mönster):
- **189 specifikt:** Tripletex aggregerar 16 underkonton till `3000` — ETL kan
  inte särskilja, ~3,2 % avvikelse på intäktssidan.
- **ClosingBalance > sum(GL-entries)** i samma SAFT-fil per
  [[warehouse-semantics]].

**Action:** Kontakta ekonomi för Tripletex-bolagen och be dem:
- Granska Tripletex-export-konfigurationen för avskrivningskonton (årlig vs
  månatlig fördelning) — primärt problem
- För 189: granska 3000-aggregeringen (16 underkonton som kollapsas)
- Om möjligt: konfigurera Tripletex att exportera periodiserade siffror

**Status om ej svar:** Accepterat brus. Påverkar inte rapporter mot
`fact_balances` direkt (rapporter använder Mercur eller IS_TOTAL) — bara
journal-jämförelsen mot Mercur-backup.

**Övriga NO-diff-klasser** (klassificerade 2026-05-27):
| Klass | Konton | # bolag | Karaktär |
|---|---|---:|---|
| A. Tripletex all-i-jan | 6010 | ~19 | Export-konfig |
| B. Periodisering | 6300, 6420, 6440, 5920, 7500 | 23-32 | Kvartalsfaktura / accrual |
| C. Kontoaggregering | 3000, 4000 | 16-34 | Mercur grupperar fler underkonton |
| D. Bolagsspecifik | 4400 (111), 5095 (36) | 1 | Reell datadiff per bolag |

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

### Verifierade 2026-05-27 (Prosero CENTR/CA + falska larm)

| Bolag | Land | Verifikation | Memory |
|---|---|---|---|
| 49 Prosero Digital Access | SE (CA) | Laddad 2026-05-26 — ren | [[project_202604_prosero_load]] |
| 50 Prosero Security AB | SE (CENTR) | Laddad 2026-05-26 — ren | [[project_202604_prosero_load]] |
| 51 Prosero Security Group | SE (CENTR) | Laddad 2026-05-26 — ren (se punkt 2) | [[project_202604_prosero_load]] |
| 52 Prosero Security AS | NO (CENTR) | Laddad 2026-05-26 — ren | [[project_202604_prosero_load]] |
| 53 Prosero Security Holding | SE (CENTR) | Laddad 2026-05-26 — ren | [[project_202604_prosero_load]] |
| 54 Prosero Denmark VB | DK (CENTR) | Laddad 2026-05-26 — ren | [[project_202604_prosero_load]] |
| 187 Prosero Security GmbH | DE (CENTR) | Laddad 2026-05-26 — ren | [[project_202604_prosero_load]] |
| 162 Doorway | SE | Var redan ren | – |
| 222 Safexit | SE | Laddad 2026-05-27 (rätt SIE-fil, clean cut 30 apr) | [[project_202604_prosero_load]], [[reference_sie_period_cutoff_gaps]] |
| 9 Beslag-Consult | NO | Var redan rätt-laddat sedan 2026-05-26 (falskt larm) | [[project_202604_prosero_load]] |
| 246 HW Mechatronic | DE | Var redan rätt-laddat sedan 2026-05-15 (falskt larm) | [[project_202604_prosero_load]] |

## Spårning

När ett externt svar kommer in:
1. Notera i `load_history`-meddelande eller annan persistent plats
2. Uppdatera den specifika punkten ovan med datum och resultat
3. Kör om relevant `load_*.py` om nya filer levererats
4. Uppdatera `_uploads/Alla bolag - jamforelse fil vs warehouse.xlsx`
