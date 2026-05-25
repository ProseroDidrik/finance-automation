# SAF-T journal-dubblett-analys 2026-05-25

**Status:** Analyserad och stängd som *mestadels uppskjuten*. 1 av 251
dubblettpar städat (Klass A); de övriga 249 är Klass B (FY-historik) som
kräver manuell triage och inte påverkar rapport-siffror — uppskjutet
medvetet.

## Bakgrund

Före `load_saft.py`-fixen `b25f397` (2026-05-21) deduppade SAF-T-loadern per
`source_file` istället för per `(company_id, period)`. Norska bolag vars
månads-SAF-T innehåller flera månaders GL (YTD-format) fick varje månad sparad
en gång per efterföljande fil. Kodfixen stoppar **nya** dubbletter; historiken
finns kvar i `fact_journal_saft`.

Rapport-siffrorna påverkas **inte** — `best_source` läser `fact_balances` som
är period-nycklat och rent. Dubbletten sitter bara i journalen och påverkar
hypotetisk framtida voucher-drilldown.

## Metodik

`scripts/analyze_saft_dup_classes.py` — klassificerar varje `(bolag, period)`
med flera källfiler i tre klasser baserat på två signaler:

1. **`loaded_at`-spann** mellan första och sista filen
2. **Filsökvägs-prefix** — `_history/` indikerar FY-historik från SIE_VER-
   omladdningen 2026-05-20

| Klass | Regel | Tolkning |
|---|---|---|
| **A** | `span > 1 dag` OCH ingen fil i `_history/` | Klassisk b25f397-bugg — månadsflöde, senare fil ÄR auktoritativ → säkert auto-städbart |
| **B** | Alla filer i `_history/` | FY-export från SIE_VER-omladdningen, alla har samma `loaded_at`-fönster, ingen tie-break möjlig → manuell triage |
| **?** | Allt däremellan | Mixad fil-profil — bedöm fall för fall |

## Resultat

Körning 2026-05-25 mot Azure Postgres, 41 av 43 NO+DK-bolag (2 timeoutar):

| Klass | Par | Rader | Bolag |
|---|---:|---:|---|
| **A** | 1 | 32 | 235 (Atech) — städad ✅ |
| **B** | 249 | 956,843 | 9, 17, 19, 36, 77, 81, 91, 103, 157, 158, 171, 175, 176, 189, 200 |
| **?** | 1 | 108 | 244 (WEO) |
| FAILED | – | – | 8 (Låshuset), 148 (Lås & Sikring Elverum) — query-timeout >10 min |

**99.2% av dubbletterna är Klass B.** Auto-städning skulle bara fixa 1 par av
251 — inte värt automatiseringen.

## Genomfört: Klass A-städning (bolag 235)

Period 202511 fick 32 rader från 4 olika månadsfiler (`2026-1`…`2026-4`),
spann 2026-05-12 → 2026-05-15. Behöll raderna från den sist laddade
(`extracted/202604/Norway/235_Atech_PO_SAF-T_2026-4.xml`), raderade 24 rader
från de tidigare tre filerna.

DELETE körd i transaktion 2026-05-25 18:25 via `_scratch/cleanup_235.py`
(scratch-skript, ej committat). Verifierat efter: 8 rader kvar för
`(235, 202511)`, alla från rätt källfil.

## Uppskjutet: Klass B-triage (249 par)

Per `_logs/saft_dup_classes_20260525_202027.csv` (gitignored) finns full
detalj. Manuell triage kräver:

1. Per bolag (15 norska + 1 DK Actas): öppna SAF-T-filerna i `_history/`
2. Bedöma vilken FY-export som är auktoritativ för varje strö-period
3. Avgöra om strå-raderna ÄR korrekta eller artefakter (öppningsbalanser,
   historiska justeringar)
4. Bygga `_params/saft_b_class_decisions.csv` med `(company_id, period, keep_source_file)`
5. Köra en triage-medveten cleanup baserat på CSV:n

Uppskattning: 5-10 timmars arbete för marginell datakvalitet, eftersom
rapport-siffror inte påverkas. **Medvetet uppskjutet.**

## Diagnostiken framåt

Det timeoutade duon (bolag 8, 148) kan analyseras separat genom att köra
skriptet med högre `statement_timeout`:

```powershell
$env:DATABASE_URL = (az keyvault secret show --vault-name kv-finauto-6427 --name database-url --query value -o tsv)
py scripts/analyze_saft_dup_classes.py --company 8 148
```

Klassificerings-trösklarna ligger som modul-konstanter i skriptet
(`SPAN_A_MIN_SEC` etc.) och kan justeras om framtida fynd motiverar det.

## Tekniska lärdomar

- **Full-tabell-GROUP-BY på `fact_journal_saft` (4.5M rader)** timeoutar
  >5 min på Burstable B1ms (120 IOPS) oavsett query-form. Per-bolag via
  `idx_fjsaft_company_period` är enda farbara vägen.
- `SET work_mem = '128MB'` + `SET statement_timeout = 600000` per session
  räcker för enskilda bolag.
- Klassificerings-heuristiken `MAX(loaded_at)` per `(bolag, period)` är
  *inte* säker — `_history/`-prefix måste in i logiken för att korrekt
  separera SIE_VER-omladdningens samma-batch-fall från månadsflödes-buggen.
- Total körning på 41 bolag tog 130 min (~2 h). Cache-träffar gör om-körningar
  mycket snabbare (bolag 9: 240s → 10s vid andra körningen).
