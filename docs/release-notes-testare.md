# Vad som hänt inför er testning — kort sammanfattning

Hej Erik och Eva! Inför att ni testar warehouse:t och GUI:t — här är vad som
förbättrats den senaste tiden. Fokus på vad ni märker som användare.

## 1. Snabbare system

Sidorna och AI-frågorna laddar märkbart snabbare än tidigare. En täckningssida
eller en fråga som förut kunde ta sin tid svarar nu i stort sett direkt. Ni
behöver inte göra något — det bara går fortare.

## 2. Sverige visar rätt belopp nu

Vissa svenska bolag levererar SIE-filer **utan #PSALDO** (periodsaldon). Utan
dem kunde warehouse:t inte räkna fram korrekta månadssiffror — det blev
felaktiga belopp och brus i avstämningen mot Mercur-facit. Vi bygger nu i
stället upp månadssiffrorna direkt från **verifikaten**. Resultatet: de svenska
bolag som tidigare såg fel ut stämmer nu mot facit.

Det finns dessutom ett filter, *"Dölj strukturellt brus"*, på täckningssidan
som sållar bort kända, ofarliga avvikelser så att verkliga differenser blir
lätta att se.

## 3. Claude förstår nu warehouse:t

Vi har "lärt" Claude (Desktop och Claude.ai) hur finance warehouse:t hänger
ihop — vilka data som finns, teckenkonventioner, skillnaden mellan YTD och
månadsvärden, vilken källa som gäller per land, osv. Det betyder att ni kan
ställa en fråga på vanlig svenska ("visa resultaträkningen för bolag X i
april") och få ett korrekt svar utan att behöva kunna datastrukturen. Claude
kontrollerar alltid uppställningen först och följer warehouse-reglerna.

## Bonus — täckningssidan

Ni kan nu se, per bolag och period, om datan är inläst *och* om den stämmer mot
Mercur-facit — med möjlighet att klicka er ända ner på kontonivå för att se
exakt var en differens sitter.

---

Hör gärna av er om något beter sig konstigt under testet — det är precis sådant
vi vill fånga.
