# Wegenetz

Erzeugt `mondschein-wege.bin`, das Wegenetz der App Mondschein für die Wanderplanung (Stufe 3),
aus den Geofabrik-Extrakten Oberbayern und Österreich und dem Höhenmodell der App. Der Workflow
`.github/workflows/wege.yml` läuft am 1. jedes Monats und auf Knopfdruck („Run workflow“) und
veröffentlicht das Wegenetz als neuen Datenstand (`daten.json` zeigt für alle anderen Dateien auf
das bisherige Release).

Die Skripte sind Kopien aus dem App-Repo (`tools/wege/wegenetz.py`, `tools/hoehen/hoehenmodell.py`,
`tools/daten/veroeffentlichen.py`); geändert wird dort, dann hierher kopiert. Format und Inhalt:
`tools/wege/README.md` im App-Repo.

Kartendaten © OpenStreetMap-Mitwirkende, ODbL 1.0. Höhen: BEV, Bayerische Vermessungsverwaltung
(CC BY 4.0), Copernicus.
