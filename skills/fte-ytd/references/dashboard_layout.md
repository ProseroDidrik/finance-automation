# Dashboard-layout — vad rapporten ska innehålla

Standalone HTML, ~500 KB, ingen internet-beroende. Bädda all data i `<script>const DATA = ...; const VALIDATION = ...; const AARO_DATA = ...;</script>`.

## Två flikar

### Flik 1: Nyckeltal

**Sammanfattnings-bullets** (top):
- Konsultkostnader Sverige YTD-delta % (Eva följer denna noga)
- Övriga externa kostnader Finland YTD-delta %
- Personalkostnadsprocent koncern YTD 2026 vs 2025
- Bruttovinstkrona per personalkrona koncern
- Bruttomarginal-delta (testar Evas mixförskjutnings-hypotes)
- Total omsättning YTD-delta
- Brutto-personal-rörelse (hires + leavers)

**Datakvalitetscallout** (warn-box):
- 36 bolag med endast helårs-SAFT 2025 → helårsproxy (FULL_YEAR_PROXY_2025), ingen YTD-apr-2025-jämförelse
- Tyska bolag med begränsad FTE-data
- Antalet bolag utan FTE
- Tecken-konventionsavvikande bolag

**KPI-kort-grid**:
- Nettoomsättning YTD 2026 (+ delta % mot YTD 2025)
- Personalkostnad YTD 2026 (+ delta %)
- Konsultkostnad YTD 2026 (+ delta %)
- Bruttoresultat YTD 2026 (+ delta %)
- Bruttomarginal YTD 2026 (vs YTD 2025)
- Personalkost % YTD 2026 (vs YTD 2025)
- Total FTE apr 2026 (vs apr 2025 + delta)
- Brutto-rörelse YTD 2026 (+hires / -leavers)

**Filter-kontroller**:
- Land-dropdown
- Sortering-dropdown (Sales, Δ Sales%, Pers%, BV/PKr, FTE, Δ FTE)
- Hide-flagged-checkbox
- Sök-textfält
- Facit-status-filter (Grön/Gul/Röd/Grå)

**Bolagstabell** (sorterbar, klickbar för drilldown):
- Bolag (med 🟢🟡🔴⚪ facit-status-dot)
- Land, Typ-badge (kons/standalone)
- Oms 2026 YTD (KSEK), Oms 2025 YTD, Δ Oms %
- BM 2026 YTD, Δ Brutto %
- Pers% av Oms, Δ Pers %, Δ Konsult %
- BV/PKr, FTE apr-26, Δ FTE
- Anställda (+/-), Slutat (+/-)
- Oms/FTE (KSEK), BV/FTE (KSEK)

**Utstickar-sektion** (filtera bort CENTR-bolag):
- Topp 5 oms-vinnare (Δ%)
- Topp 5 oms-tappare (Δ%)
- Topp 5 personalkost-ökning
- Topp 5 konsultkost-ökning
- Topp 5 bruttovinst-tappare
- Topp 5 högsta personalkost%
- Topp 5 nettoanställningar
- Topp 5 nettominskningar

### Flik 2: Validering mot Mercur

(Bara om facit-fil bifogats)

**Sammanfattning**: X av Y bolag inom ±5%, klassificeringsskillnader-förklaring

**KPI-kort** (per top_group): Facit MSEK, Warehouse MSEK, Diff%

**Koncerntotal-tabell**: per top_group facit vs WH-RU vs Δ% med status-dot

**Per-RU-tabell**: sorterbar diff%-tabell med color-coded cellfärger

**Klassificeringsanalys per aaro-konto**: filterbar tabell över alla aaro-rader med facit-värde vs warehouse-värde, ger insikt om var pengarna ligger när top_group-totaler matchar men sub-rader inte gör det

**Omappade bolag-lista**: bolag i Mercur som inte mappades till warehouse

## Visuella regler

- Färgkod diff%: grön (≤1%), gul (1-5%), röd (>5%), grå (NA)
- Sales positiv = bra (grön delta), kostnad positiv = dåligt (röd delta)
- Tooltips på status-dots med exakta värden
- Mobil-vänlig är inte nödvändig — Eva använder desktop
- Default-sortering: Total Sales YTD 2026 fallande

## CSS-variabler för konsistens

```css
:root {
  --bg: #f7f8fa;
  --card: #ffffff;
  --border: #e4e6eb;
  --text: #1a1a1a;
  --muted: #6b7280;
  --accent: #2563eb;
  --good: #059669;  /* grön */
  --warn: #d97706;  /* gul */
  --bad: #dc2626;   /* röd */
  --neutral: #4b5563;
}
```

## JS template-fällan att undvika

I template literals stänger `}` ett `${}` block. Skriv inte:
```javascript
`${cond ? '—' : value.toFixed(1)+' pe})`  // FEL: } inuti string stänger ${}
```

Skriv:
```javascript
`(${cond ? '—' : value.toFixed(1)+' pe'})`  // OK: stäng quote före }
```

Validera alltid med `node --check` innan leverans.
