# Wald am Weg

Erzeugt `mondschein-wald.bin` für das Mondlicht am Weg der App Mondschein (Meilenstein 23): je 1/3″-Zelle
(ca. 10 m × 7 m) an den Wegen der neuesten Wegedatei die Lückenquote und Kronenhöhe aus Meta/WRI CHMv2
und, falls im Release `quellen` eine Copernicus-DLT-Datei liegt, den Blatttyp. Der Workflow
`.github/workflows/wald.yml` läuft nur auf Knopfdruck („Run workflow“) und veröffentlicht die Datei im
Datenstand des Tages.

`wald.py` ist eine Kopie aus dem App-Repo (`tools/licht/wald.py`); geändert wird dort, dann hierher kopiert.
Format, Rechnung und wie man die DLT-Datei beschafft: `tools/licht/README-wald.md` im App-Repo.

Canopy height: Meta and World Resources Institute (2026), CHMv2, CC BY 4.0, verändert.
Generated using European Union's Copernicus Land Monitoring Service information (Blatttyp).
