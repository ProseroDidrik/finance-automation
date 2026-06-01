# Mappning: Mercur-namn → warehouse company_id

Mercur Resultaträkning-exporten har bolagsnamn i kolumn-headers. Dessa matchar inte alltid 1:1 mot `dim_company.name` i warehouse. Tabellen nedan är hämtad från flera iterationer av manuell verifiering.

## Hur man bygger mappningen

1. **Exakt namnmatch först** — `dim_company.name` (lowercase + strip suffixes som AB, AS, OY) jämfört med Mercur-kolumnnamn likadant normaliserat.
2. **MANUAL-tabell** för kända specialfall (se nedan)
3. **Token-overlap fuzzy** för resten — kräver minst 0.5 overlap-ratio

## Manuell mappnings-tabell (Python-dict)

```python
MERCUR_TO_CID = {
    # Prosero centrala bolag
    'Prosero Security AS': 52,
    'Prosero Security AB': 51,
    'Prosero Security GmbH': 187,
    'Prosero Security Group AB': 51,
    'Prosero Security Holding AB': 53,
    'Prosero Secuity OY': 145,           # OBS: typo "Secuity" är så det faktiskt heter i warehouse
    'Prosero Doorway': 162,
    'Prosero Security Denmark A/S': None,  # ej i warehouse — skip

    # Konsoliderade enheter (parent_id på subs pekar hit)
    'Axlås & Begelås konsoliderad': 101,
    'Passera konsoliderat': 160,
    'OpenUp & Montageservice Konsoliderat': 154,
    'Dalek & Sotenäs Konsoliderat': 22,
    'Dala Lås konsoliderat': 24,
    'Lås & Nyckel i Gävle konsoliderat': 27,
    'Lås & Nyckel Gävle konsoliderat': 27,
    'Säkerhetsteknik konsoliderat': 29,
    'Säkerhetsteknik & All-Round & ADS & SKT Konsoliderat': 29,
    'Sickla Lås gruppen': 138,
    'Sickla Låsgruppen konsoliderat': 138,
    'Norsk Brannvern konsoliderat': 140,
    'Norsk Brannvern konsoliderad': 140,
    'Buysec-Buytec konsoliderat': 174,
    'Sikring Nord konsoliderat': 192,
    'Assistent Partner konsoliderat': 203,
    'Assistent Partner konsoliderad': 203,
    'Brann og Sikrings. & Lås og Beslag konsoliderat': 206,
    'Brann og Sikringsservice & Lås og Beslag konsoliderad': 206,
    'Norrskydd konsoliderat': 213,
    'Norrskydd konsoliderad': 213,
    'Safeexit konsoliderat': 225,
    'Safexit konsoliderad': 225,         # OBS: stavfel i Mercur
    'Sundsvall konsoliderat': 227,
    'Romerike konsoliderat': 228,
    'Kungälv & Säkerhetspartner konsoliderat': 241,
    'Actas konsoliderad': 132,
    'Actas A/S konsoliderat': 132,

    # Tyska bolag (med långa officiella namn i Mercur)
    'Weckbacher Sicherheitssysteme GmbH': 220,
    'Franz Mittermeier GmbH': 231,
    'H+W Mechatronik GmbH': 246,
    'Goldfunk Sicherheitstechnik GmbH': 245,
    'Bofferding GmbH': 188,

    # NO-bolag med mappningsfel som krävt manuell fix
    'Låsservice Stavanger AS': 233,
    'Ålesund': 77,                       # KRITISK: detta är cid 77 (Låsservice Ålesund), INTE cid 80 (Lockit)
    'Lås & Sikring AS (Elverum)': 148,   # cid 148, INTE cid 16 (Tromsø)
    'Lås & Sikring AS Namsos': 217,
    'Aker Lås og Nøkkel AS': 78,
    'Asker Lås': 158,
    'Hemer Lås & Dørtelefon AS': 157,
    'Nordland Lås & Sikkerhet AS': 165,
    'Låsesmeden Finnsnes AS': 171,
    'Lofoten låsservice AS': 200,
    'JM Lukko - Ja Turvatekniikka Oy': 221,
    'WEO Lås & Sikkerhets AS': 244,
    'THV Teleja Hälytysvalvonta Oy': 182,
    'Meri Lapin Lukituspalvelu Oy': 195,
    'Turvatalo - Tapiolan Yleishuolto Oy': 153,

    # SE-bolag med längre Mercur-namn än warehouse-namn
    'Tele & Säkerhetstjänst i Skara AB': 6,
    'Låssmeden Sven Alexandersson AB': 14,
    'Cadsafe Brandservice AB': 21,
    'Creab säkerhet AB': 105,
    'Exista Säkerhet AB': 151,
    'El & Fastighetsdrift Stockholm AB': 164,
    'Safetytech i Väst AB': 23,
    'Hässleholms Låssmed AB': 93,
    'Larmatic Alarm AB': 110,
    'Södra Vägens Låsservice AB': 152,
    'Norrbottens Larmkonsult AB': 172,
    'Uppsala Säkerhetsteknik AB': 180,
    'Låssmeden KanLås AB': 186,
    'Lås-Arne Malmström AB': 197,

    # DK
    'SIKOM Danmark A/S': 216,

    # Skip — elimineringsbolag eller jämförelsekolumner i Mercur, ej operativa enheter
    'Utfall fg. år': None,
    'Elimineringsbolag Sverige': None,
    'Elimineringsbolag Norge': None,
    'Elimineringsbolag Finland': None,
}
```

## Hantering av mappningsdubbletter

Om Mercur har flera kolumner som mappar till samma cid (t.ex. 'Lockit AS' och 'Ålesund' båda → cid 80), är minst en mappning fel. Verifiera mot Mercurs faktiska kolumn med data.

## Hantering av konsoliderade enheter

För `kind='consolidated'` reporting unit:
- `member_cids = [parent_cid] + [alla cid med parent_id = parent_cid]`
- Vid aggregering: summa över alla member_cids
- I dashboarden: visa bara den konsoliderade enheten, inte sub-bolagen separat

## Bolag som INTE finns i Mercur men finns i warehouse

Vissa warehouse-bolag rapporteras inte i Mercur Resultaträkning (mindre/avvecklade bolag). Visa dem som "grå dot" i dashboarden = "ej i Mercur-rapporten", inte som "röd" = "stor diff".
