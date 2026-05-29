# Nytt sedan sist — uppdatering till er testning

Hej Erik och Eva! Sedan förra sammanfattningen har en del hänt. Här är det ni
märker som användare — i klartext, utan teknik.

## 1. Ni kan nu bryta ner siffrorna på dimensioner (kostnadsställe, avdelning)

Tidigare fick ni resultatet per bolag och konto. Nu finns även bolagens egna
**dimensioner** med — i den mån bolaget har bokfört på dem. Det vanligaste är
**kostnadsställe** (svenska bolag) och **avdelning** (norska bolag); enstaka
bolag har även andra axlar. Det gäller både SIE-bolagen (Sverige) och
SAF-T-bolagen (Norge/Danmark).

Det betyder att ni kan fråga Claude t.ex. "visa kostnaderna per kostnadsställe
för bolag X i mars" och få svar på den nivån — för de bolag som taggat sina
poster. Tänk på att täckningen varierar mellan bolag: bara poster som faktiskt
är dimensionstaggade syns, så en uppdelning visar sällan 100 % av beloppet.
Dimensionerna finns även bakåt i tiden, inte bara för i år.

## 2. Norska och danska månadssiffror stämmer nu

Vissa poster — typiskt årets avskrivningar — hamnade tidigare felaktigt i
**januari** i stället för att fördelas på rätt månad. Det gjorde att enskilda
månader kunde se konstiga ut jämfört med Mercur-facit. Det är nu rättat: varje
post landar på den månad den faktiskt hör till, och de norska och danska bolagen
stämmer mot facit månad för månad.

## 3. Stadig grund: vi läser de officiella standardfilerna

Siffrorna byggs numera direkt från de **officiella standardformaten** för
bokföringsdata, inte från godtyckliga eller hemmasnickrade exporter:

- **SAF-T** för de norska och danska bolagen — det är filformatet som
  **skattemyndigheten kräver** (i Norge: Skatteetaten) för bokföringen.
- **SIE** för de svenska bolagen — den **svenska redovisningsstandarden** som i
  princip alla bokföringsprogram stödjer.

Det är stabila, väldefinierade källor med samma struktur oavsett vilket
bokföringssystem bolaget kör. I praktiken: en pålitlig grund att lita på, och
färre överraskningar i avstämningen mot facit.

## 4. P&L-fliken: välj källa direkt i gränssnittet

I förra noten skrev vi att P&L-fliken bara räknade på grundsiffrorna, och att ni
fick fråga Claude om ni ville ha de manuella justeringarna inräknade. Det är nu
löst i webbgränssnittet: i P&L-pivoten finns en **källväljare** där ni själva
bockar för om grundsiffror, manuella justeringar och importjusteringar ska räknas
med — var för sig eller tillsammans. Ni ser hela bilden utan att lämna fliken.

## 5. Personal — ytterligare ett bolag

Personal-fliken täcker nu även Goldfunk. I övrigt fungerar den som tidigare:
antal anställda per bolag, samt hur många som börjat respektive slutat under året.

---

Hör gärna av er om något beter sig konstigt — det är precis sådant vi vill fånga
i testet. Tack för hjälpen!
