# Wolken für Mondschein

Kopie von `tools/wetter/icon_d2.py` aus dem (privaten) App-Repo. Der Workflow
`.github/workflows/wolken.yml` bereitet alle 3 Stunden den neuesten Lauf des DWD-Modells ICON-D2 auf
und legt das Ergebnis im Release **`wolken`** ab:

- `mondschein-wolken.bin` – 3D-Wolkenanteil für das Gebiet der App, 49 Vorhersagestunden (Format im Kopf
  von `icon_d2.py`), ca. 12–13 MB
- `wolken.json` – Lauf, Größe und SHA-256 der Datei

Feste Adressen (die App lädt dort):

- https://github.com/lh1000/Mondschein-Daten/releases/download/wolken/wolken.json
- https://github.com/lh1000/Mondschein-Daten/releases/download/wolken/mondschein-wolken.bin

Das Release `wolken` ist als Vorabversion markiert, damit „neuestes Release“ weiterhin der Datenstand
(Karte, Höhenmodell) bleibt.

Daten: Deutscher Wetterdienst, CC BY 4.0 („Quelle: Deutscher Wetterdienst“), verändert
(ausgeschnitten, gemittelt, gerundet).
