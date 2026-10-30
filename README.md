# Mondschein – Datendateien

Offline-Daten für die Android-App **Mondschein** (wo sind Mond- und Sonnenauf- bzw. -untergang über
dem Gelände sichtbar?). Gebiet: Nördliche Kalkalpen von den Ammergauer Alpen bis Berchtesgaden,
mit Inntal, Nordkette und Alpenvorland bis München.

Die Dateien liegen als **Releases** vor; `daten.json` im neuesten Release listet Name, Größe und
SHA-256-Prüfsumme jeder Datei. Die App lädt daraus nur, was sich geändert hat.

| Datei | Inhalt |
|---|---|
| `mondschein.pmtiles` | Grundkarte (Vektorkacheln) |
| `mondschein-wandern.pmtiles` | Wander-Ebene: Wege mit Schwierigkeit, Routen, Hütten, Gipfel |
| `mondschein-hoehen.bin` | Höhenmodell: Kern 1/3″ (ca. 10 m), Horizontpuffer 3″ (ca. 90 m) |
| `mondschein-schummerung.pmtiles` | Schräglicht-Schummerung |
| `mondschein-hoehenlinien.pmtiles` | Höhenlinien |
| `mondschein-wald.bin` | Wald am Weg: Lückenquote, Kronenhöhe, Blatttyp je 10-m-Zelle an den Wegen |

## Quellen und Lizenzen

Die Dateien sind aus den folgenden Quellen **abgeleitet und verändert** (zugeschnitten, umprojiziert,
gemittelt bzw. maximiert, in Kacheln umgewandelt):

- **Kartendaten:** © OpenStreetMap-Mitwirkende, Lizenz [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/) –
  [openstreetmap.org/copyright](https://www.openstreetmap.org/copyright). Kartenkacheln der Grundkarte erzeugt mit
  [Protomaps](https://protomaps.com). Die Kartendateien (`*.pmtiles` außer Schummerung und Höhenlinien) stehen
  als abgeleitete Datenbank ebenfalls unter der ODbL.
- **Höhendaten Österreich:** Datenquelle BEV – Bundesamt für Eich- und Vermessungswesen, ALS DTM (1 m),
  Lizenz [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.de), verändert.
- **Höhendaten Bayern:** Bayerische Vermessungsverwaltung – [www.geodaten.bayern.de](https://www.geodaten.bayern.de),
  DGM1, Lizenz [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.de), verändert.
- **Copernicus DEM GLO-30:** © DLR e.V. 2010–2014 und © Airbus Defence and Space GmbH 2014–2018, bereitgestellt
  im Rahmen von COPERNICUS durch die Europäische Union und die ESA.
- **Höhendaten Schweiz und Liechtenstein:** swissALTI3D © swisstopo (Open Government Data), verändert (gemittelt).
- **Höhendaten Baden-Württemberg:** Datenquelle LGL, [www.lgl-bw.de](https://www.lgl-bw.de), DGM1, Lizenz
  [dl-de/by-2-0](https://www.govdata.de/dl-de/by-2-0), verändert (gemittelt und umprojiziert).
- **Höhendaten Elsass:** IGN – RGE ALTI, [Licence Ouverte Etalab 2.0](https://www.etalab.gouv.fr/licence-ouverte-open-licence/), verändert (gemittelt).
- **Höhendaten Niederlande:** AHN – Actueel Hoogtebestand Nederland (Rijkswaterstaat, Waterschappen), CC0, verändert (gemittelt).
- **Höhendaten Niedersachsen:** © GeoBasis-DE / LGLN 2026, DGM1, Lizenz [dl-de/by-2-0](https://www.govdata.de/dl-de/by-2-0),
  verändert (gemittelt und umprojiziert).
- **Regionen der Gebietserweiterung** (`mondschein-<art>-<region>.*`, Höhenkacheln `mondschein-hoehen-N47E010.bin`):
  dieselben Quellen; Grundkarten als Protomaps-Ausschnitte, Wege und Wander-Ebene aus OpenStreetMap
  (Geofabrik-Extrakte, ODbL 1.0); wo keine amtlichen Höhendaten genutzt werden, Copernicus DEM GLO-30.
- **Wald (`mondschein-wald.bin`):** Canopy height: Meta and World Resources Institute (2026), CHMv2,
  Lizenz [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.de), verändert (Lückenquote und Kronenhöhe je
  1/3″-Zelle). Blatttyp, falls enthalten: Generated using European Union's Copernicus Land Monitoring Service
  information, HRL Dominant Leaf Type, doi:10.2909/82f93572-9888-47ef-97a1-5cac5985a26a.

Höhenmodell, Schummerung und Höhenlinien dürfen unter denselben Bedingungen weiterverwendet werden
(Namensnennung der genannten Quellen, Hinweis auf Veränderung).
