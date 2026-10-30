# Wegenetz (Stufe 3)

`wegenetz.py` erzeugt `mondschein-wege.bin`, das Wegenetz für die Wanderplanung (docs/konzept.md,
Stufe 3, Meilenstein 15). Die App liest es mit `core/.../wege/Wegenetz.kt`; die Wegsuche mit Gehzeit
(Meilenstein 16) steht in `Wegsuche.kt` und `Wanderzeit.kt`, Herleitung in docs/mathe.md, Abschnitt 12.

## Inhalt

- **Wege:** `highway` = path, footway, track, steps, bridleway, via_ferrata, pedestrian sowie die
  Zubringer unclassified, service (ohne Parkplatzgassen und Einfahrten), residential, living_street,
  tertiary. Ohne `foot=no` und ohne `access=no/private` (außer mit `foot=yes/designated/permissive`).
  Ein Weg wird ganz übernommen, wenn ein Punkt im Kerngebiet (47,25–47,90° N, 10,70–13,15° O) liegt.
- **Knoten:** Wegenden, Kreuzungen (OSM-Knoten in mehr als einem Weg) und Startpunkte.
- **Kanten:** Wegstücke zwischen zwei Knoten, in beiden Richtungen begehbar. Länge entlang aller
  OSM-Punkte; Auf- und Abstieg aus dem Höhenmodell der App (`mondschein-hoehen.bin`, Höhen alle
  20 m entlang des Stücks, bilinear wie `Hoehenmodell` in core); Wegart, `sac_scale`,
  `trail_visibility`, Name oder Wegnummer.
- **Geometrie:** Zwischenpunkte für Anzeige und Abtastung; nur Punkte, die weniger als 0,1 m neben
  der Geraden ihrer Nachbarn liegen, entfallen (Douglas-Peucker). Mit 2 m Toleranz wurden Stufen und
  enge Kehren um bis zu 9 % kürzer als die Kante.
- **Startpunkte:** Parkplätze (`amenity=parking`, nicht privat oder Tiefgarage), Haltestellen,
  Bahnhöfe, Hütten (`alpine_hut`, `wilderness_hut`), Seilbahnstationen, jeweils am nächsten Wegpunkt
  (bis 300 m), der dafür zum Knoten wird.
- **Orte mit Aussicht** (Meilenstein 18b): Gipfel (`natural=peak`) und Sättel bzw. Pässe (`natural=saddle`,
  `mountain_pass=yes`) mit Namen, Aussichtspunkte (`tourism=viewpoint`) und Hütten, mit OSM-Höhe (`ele`).
  Testausschnitt: `test-orte.osm` → `core/src/test/resources/wege-orte.bin`.
- **Gipfelkreuze** (Meilenstein 38): Gipfel mit `summit:cross=yes|cross` oder mit einem eigenen Kreuz-Knoten
  (`man_made=cross`) im Umkreis von 80 m (wie in der Wander-Ebene); Merkmal im Byte 17 des Orts.
- **Zufahrt zu Parkplätzen** (Meilenstein 19a): Aus allen Straßen bis 0,3° um das Gebiet entsteht das
  Netz für private Autos. Gesperrt sind `motorcar`/`motor_vehicle`/`vehicle`/`access` (in dieser
  Reihenfolge, die genaueste Angabe gilt) = no, private, agricultural, forestry, destination (nur
  Anlieger), delivery, permit, emergency, military, customers, sowie Forstwege (`track`) ohne
  ausdrückliche Freigabe. Ein Parkplatz hat Zufahrt, wenn eine freie Straße bis 80 m neben ihm (sonst
  die nächste bis 300 m) zum größten zusammenhängenden Teil dieses Netzes gehört. Zusätzlich: nur über
  Mautstraßen (`toll=yes`) bzw. nur über zeitweise gesperrte Straßen (`…:conditional` = no …).
  Testausschnitt: `test-zufahrt.osm` → `core/src/test/resources/wege-zufahrt.bin`.
- **Nur Gehzeiten:** Seil-, Zahnrad- und Standseilbahnen sind keine Kanten. Bahnhalte und Bahnsteige
  bis 60 m neben einem Gleis mit `rack=*`/`railway:rack=*` oder `railway=funicular` (Wendelstein,
  Mitteralm, Zugspitze, Rosshütte) werden wie Seilbahnstationen als „Bergbahnstation“ geführt; die App
  bietet sie nie als Start an. Bushaltestellen bleiben Haltestellen, auch neben einem solchen Gleis.

Quelle: dieselben Geofabrik-Extrakte wie die Wander-Ebene (tools/karte/README.md). Kartendaten
© OpenStreetMap-Mitwirkende, ODbL 1.0.

## Aufruf

Python-Umgebung aus tools/hoehen (braucht `osmium`, `numpy`, `scipy`; alle schon für die Karte installiert):

```
%LOCALAPPDATA%\Mondschein\venv\Scripts\python wegenetz.py --pbf %LOCALAPPDATA%\Mondschein\osm\oberbayern-latest.osm.pbf %LOCALAPPDATA%\Mondschein\osm\austria-latest.osm.pbf --hoehen ..\..\..\Daten\mondschein-hoehen.bin --ausgabe ..\..\..\Daten\mondschein-wege.bin
```

Die Testdatei in core (Ausschnitt Bayrischzell/Wendelstein) mit `--gebiet 47.665,47.71,11.99,12.04
--ausgabe ..\..\core\src\test\resources\wege-wendelstein.bin`; nur Oberbayern nötig.

## Format `MSWEGE1` (Little Endian, Abschnitte auf 4 Byte aufgefüllt)

| Abschnitt | Inhalt |
|---|---|
| Kopf, 64 Byte | `MSWEGE1\0`, Version (int32 = 1), Knoten N, Kanten M, Geometriepunkte P, Startpunkte S, Größe der Namen in Byte, Stand JJJJMMTT, Orte O, Beginn der Orte in Byte (alle int32), Rest 0 |
| Knoten, N × 12 Byte | Breite, Länge (int32, Mikrograd), Höhe (int32, Dezimeter) |
| Kanten, M × 28 Byte | von, nach (int32), Länge (float32, m), Aufstieg, Abstieg von → nach (uint16, dm), Wegart, SAC 0–6, Sichtbarkeit 0–6, frei (je uint8), erster Geometriepunkt (int32), Name (int32, Byte-Index in die Namen, −1 = keiner) |
| Geometrie, P × 4 Byte | Zwischenpunkte der Kanten der Reihe nach als Schritte (int16 Breite, int16 Länge) in 1/100 000 Grad, beginnend beim Knoten `von`, auf dieses Raster kaufmännisch gerundet; die Punkte einer Kante reichen bis zum ersten Punkt der nächsten |
| Startpunkte, S × 20 Byte | Breite, Länge (int32, Mikrograd), Knoten (int32), Name (int32), Art (uint8: Parkplatz, Haltestelle, Bahnhof, Hütte, Seil- oder Bergbahnstation), Zufahrt (uint8, Bits: 1 keine Zufahrt, 2 nur über Maut, 4 zeitweise gesperrt; seit 28.10.2026, vorher 0), 2 Byte frei |
| Namen | je Name Länge (uint16) und UTF-8 |
| Orte, O × 20 Byte (seit 28.10.2026) | Breite, Länge (int32, Mikrograd), OSM-Höhe (int32, Dezimeter, −2³¹ = unbekannt), Name (int32), Art (uint8: Gipfel, Sattel/Pass, Aussichtspunkt, Hütte), Merkmale (uint8, Bit 0 = Gipfelkreuz, seit Meilenstein 38; vorher 0), 2 Byte frei. Ältere Leser kennen den Abschnitt nicht und überspringen ihn; ältere Dateien haben O = 0. |

Wegart (uint8): Pfad, Fußweg, Fahrweg (`track`), Stufen, Reitweg, Klettersteig, Fußgängerzone, Straße.
