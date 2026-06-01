# Kända fallgropar — läs detta först

Lärt från flera iterationer. Varje punkt har orsakat bug i tidigare körningar.

## 1. SIE_PSALDO är monthly, inte ytd

**Symptom:** Total Sales hamnar ~25-35% av facit-värdet. Ser ut som "stale" data men är felaktig CTE-logik.

**Orsak:** `period_type='monthly'` på SIE_PSALDO men många skriver `WHERE period = '202604'` som om det vore YTD. Detta ger bara aprils rörelse.

**Fix:** I CTE, behandla SIE_PSALDO som IMP — summera jan..period:
```sql
WHERE (bp.base_src IN ('SIE','SIE_VER','SAFT') AND fb.period = target_period)
   OR (bp.base_src IN ('SIE_PSALDO','IMP') AND fb.period BETWEEN '<yyyy>01' AND target_period)
```

**Verifiering:** Kontrollera mot ett bolag du vet värdet på. För LH Electronic Alarm (cid 11) ska Total Sales YTD 202604 vara ~36 MSEK.

## 2. Dubbelräkning av MAN för NO YTD 2025

**Symptom:** NO YTD 2025 i totalsumman är ~2x för högt.

**Orsak:** Min query för base_pick + adj_ytd ger MAN-värdet (eftersom SAFT saknas i fact_balances för 2025-01..11). Plus separat journal_saft-syntes innehåller också MAN-effekter via verifikat. Båda summeras → MAN räknas dubbel.

**Fix:** Antingen
- Använd ENBART journal_saft-syntes som NO 2025-värde (lägg på MAN/IMP_ADJ separat från fact_balances)
- Eller använd ENBART ytd_combined (acceptera att SAFT-bidrag saknas)
- Inte båda

## 3. CENTR-bolag tävlar i utstickar-listor

**Symptom:** Listor över "topp 5 oms-tappare" eller "personalkost-ökningar" domineras av elimineringsbolag (Prosero Security AS/AB/GmbH/Holding, Elimineringsbolag Central). Inte intressant för Eva.

**Fix:** I `renderOutliers()` JS, filtrera bort `r.country === 'CENTR'`:
```javascript
const clean = rs.filter(r => !r.flags.some(f => 
  ['NEG_SALES_2026','NEG_SALES_2025','NO_SALES_DATA'].includes(f)
) && r.country !== 'CENTR');
```

CENTR-bolagen syns fortfarande i huvudtabellen (filtrerbar) — bara inte i ranking-listor.

## 4. JS template literal-fällan

**Symptom:** Dashboard renderar inget. JS SyntaxError. Specifikt: `${ ... + ' pe})` — `}` stänger `${}` för tidigt.

**Fix:** Stäng quote före }:
```javascript
`(${ppDelta == null ? '—' : (ppDelta>=0?'+':'')+ppDelta.toFixed(1)+' pe'})`  // ' pe' med stängande quote
```

**Verifiering före leverans:** `node --check /tmp/dashboard.js` på den embedded JS:en.

## 5. dim_company.parent_id behövs för RU-bygge

**Symptom:** Konsoliderade enheter har 0 data i dashboarden trots att deras subs har data.

**Orsak:** SQL-query för dim_company sparade inte `parent_id`-kolumnen.

**Fix:** Inkludera explicit:
```sql
SELECT json_agg(json_build_object(
  'company_id', company_id, 'name', name, 'country', country, 
  'kind', kind, 'currency', currency, 'parent_id', parent_id
))::text AS payload FROM dim_company;
```

## 6. Tecken-konvention varierar per bolag, inte land

**Symptom:** Vissa bolag (t.ex. Actas DK) får felaktig Total Sales eftersom de använder SIE-konvention (intäkt negativ) trots att DK normalt är Mercur-konvention (intäkt positiv).

**Fix:** Använd `abs()` per (cid, period, top_group) istället för att försöka klassa per land:
```python
amt = abs(raw_amount) * fx_rate
```

Detta är "positiv magnitud"-normalisering och fungerar för Sales/cost lika.

## 7. Mappningsfel som ser ut som data-fel

**Symptom:** Vissa NO-bolag har stora diff mot facit (>100%). Ser ut som data-problem.

**Faktisk orsak:** Mappnings-fel — Mercur-namnet pekar på fel warehouse cid.

**Klassiska exempel:**
- "Ålesund" → cid 80 (Lockit) **FEL**, ska vara cid 77 (Låsservice Ålesund)
- "Lås & Sikring AS (Elverum)" → cid 16 (Tromsø) **FEL**, ska vara cid 148 (Elverum)
- "Prosero Security AS/AB/GmbH" alla mappade till samma cid → fel

**Förebyggande:** Innan du flaggar något som data-fel, kontrollera att namnmappningen är rätt genom att kolla `dim_company.name ILIKE '%<mercur-namn-fragment>%'`.

## 8. Output-storlek från query_sql

**Symptom:** Query lyckas men returnerar bara preview (50 rader).

**Fix:** Antingen
- Använd `json_agg(row_to_json(t))::text` för payload — returneras som en string
- Stort output sparas automatiskt till `tool-results/<id>.txt`, läs in via Python
- Aggregera redan i SQL (per land istället för per bolag etc.)

## 9. CENTR-bolagens valuta — FIXAD I PROD 2026-06-01 (override ej längre nödvändig)

**Status:** Rättat vid källan. `db.py` har nu `COMPANY_CURRENCY_OVERRIDE` (50/51/53→SEK, 52→NOK, 54→DKK) som appliceras efter `COUNTRY_CURRENCY`-defaulten i `sync_dim_company`; `dim_company.currency` är korrekt i prod (verifierat). **Skillen behöver INGEN egen currency-override längre** — `dim_company.currency` kan användas rakt av. Texten nedan behålls som historik.

---



**Symptom:** CENTR Personnel-totalen blåses upp ~10x (lookout för 100+ MSEK över facit). Total Sales är mindre påverkad eftersom CENTR-bolagen har lite intäkter.

**Rot-orsak:** Alla 8 CENTR-bolag har `currency='EUR'` i dim_company. Men orgnr-formaten visar att bara cid 145 (FI) och 187 (DE) faktiskt är EUR. De andra:
- cid 50, 51, 53 (Prosero Security AB/Group AB/Holding) → SEK (svenska orgnr 559108-xxxx)
- cid 52 (Prosero Security AS) → NOK (norskt orgnr 9 siffror)
- cid 54 (Prosero Security A/S) → DKK (danskt orgnr 8 siffror)
- cid 60 (Elimineringsbolag Central) → EUR (eliminering)

När du multiplicerar SEK-belopp med EUR→SEK-rate 10.69 får du ~10x för stora värden.

**Fix — hardkoda override i Python:**

```python
CURRENCY_OVERRIDE = {
    50: 'SEK', 51: 'SEK', 52: 'NOK', 53: 'SEK', 54: 'DKK',
    60: 'EUR', 145: 'EUR', 187: 'EUR',
}
# Apply på dim_company och ytd-rader innan FX-omräkning
for c in companies:
    if c['company_id'] in CURRENCY_OVERRIDE:
        c['currency'] = CURRENCY_OVERRIDE[c['company_id']]
for r in ytd_rows:
    if r['company_id'] in CURRENCY_OVERRIDE:
        r['currency'] = CURRENCY_OVERRIDE[r['company_id']]
```

**Förebyggande:** Detta är en data-bug i `dim_company`. Be Claude Code uppdatera `_params/Dotterbolagslistan.xlsx` så CENTR-bolagens valuta-kolumn matchar deras faktiska bokföringsvaluta. Sen kan denna override tas bort.

**Effekt efter fix (verifierat):** Personnel-total från 698 → 554 MSEK (matchar facit), Consultants från 47 → 24, Other External från 131 → 79.

## 10. Mercur har dubblettkolumner

**Symptom:** Total Sales i facit är ~2x för högt för ett bolag (t.ex. "Axlås & Begelås konsoliderad" var 73M istället för 38M).

**Orsak:** Mercurs Resultaträkning-export har samma bolag som flera kolumner (samma namn).

**Fix:** Dedup på kolumnnamn — bara första förekomsten räknas:
```python
seen = set(); unique_cols = []
for i, h in enumerate(hdr):
    if not h: continue
    h = h.strip()
    if h in seen: continue
    seen.add(h)
    unique_cols.append((i, h))
```

## 11. journal_saft är inte tillförlitlig för 2025-syntes

**Symptom:** Du försöker syntetisera 2025 YTD apr för bolag som saknar månadsvis SAFT-data i fact_balances genom att summera `reporting.journal_saft` per (account_code, period). Resultatet hamnar långt från facit (~1% av rätt värde).

**Rot-orsak:** `fact_journal_saft` är endast ~6% inläst för 2025. Pipelinen som plockar in journal-rader på radnivå har inte körts färdigt. Aggregat från denna källa är därför inte representativa.

**Fix:** Sluta syntetisera ur journalen. Använd istället SAFT 202512 (helår) som proxy där det finns.

**Status (v1.4, 2026-06-01):** Syntesen är BORTTAGEN ur koden — `NO_YTD_2025_SYNTH_QUERY` raderad ur `sql_queries.py` och `no_2025`-mergen raderad ur `build_ru_aggregat.py`. Berörda bolag hanteras nu via helårsproxy, se #12.

## 12. 36 bolag saknar månadsvis SAFT för 2025 — använd helårs-proxy

**Symptom:** Vissa bolag (mest NO + Actas DK) har `source_kind='SAFT'` endast för `period='202512'` och inget för 202501..202511. Försöker man göra "YTD apr 2025" från den datan får man 0.

**Rot-orsak:** Historisk SAF-T laddas årsvis (YYYY12), inte månadsvis — bara helåret finns för 2025. Det går alltså inte att räkna ut en YTD-apr-2025-siffra; datan existerar inte på den granulariteten.

**Fix (v1.4):** De 36 bolagen är hårdkodade i `build_ru_aggregat.FULL_YEAR_ONLY_2025` (verifiera mot prod, se query nedan). För dessa:
- Flagga `FULL_YEAR_PROXY_2025`.
- Nulla financial-YoY mot 202504 (den baslinjen finns inte) — `sales_abs/pct`, `personnel_*`, `consultants_*`, `other_ext_*`, `gp_*` sätts None.
- Behåll `fte_delta` (personalsnapshot från `reporting.personnel` är oberoende av SAFT-balanser).
- Använd bolagets egna 202512-SAFT (redan i `ytd_topgroup_allkinds.json`) som helårsvärde och jämför mot Mercurs HELÅRSSIFFRA, inte YTD apr.

Verifieringsquery för listan:
```sql
SELECT company_id FROM fact_balances
WHERE source_kind='SAFT' AND scenario='A' AND period BETWEEN '202501' AND '202512'
GROUP BY company_id HAVING COUNT(DISTINCT period)=1 AND bool_or(period='202512')
ORDER BY company_id;
```
Avviker resultatet från de 36 i koden → någon SAFT har laddats om; uppdatera `FULL_YEAR_ONLY_2025`.