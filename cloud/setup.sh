#!/usr/bin/env bash
# Einrichtung einer Claude-Code-Cloud-Sitzung für Mondschein (docs/arbeiten-ohne-pc.md).
#
# Läuft als Setup-Skript der Cloud-Umgebung (als root, Ubuntu 24.04, vor dem Start von Claude) und
# wird danach als Abbild zwischengespeichert, solange es unter ca. 5 Minuten bleibt. Installiert:
#   - Android-SDK (Kommandozeilen-Werkzeuge, Plattform 37, Build-Tools 36) nach /opt/android-sdk
#   - Datendateien aus den Releases von lh1000/Mondschein-Daten nach /opt/mondschein/Daten
#     (dieselbe Ordnerstruktur wie Mondschein/Daten am PC; die Tests mit echten Daten laufen dann mit)
#   - Python-Pakete für tools/ (osmium, numpy, scipy, rasterio …) in /opt/mondschein/venv
# Netz: Standard-Freigaben („Trusted“) plus dl.google.com für das Android-SDK.
#
# In der Umgebung als Setup-Skript eintragen: diesen ganzen Inhalt einfügen, oder (sobald die Kopie im
# öffentlichen Daten-Repo liegt) eine Zeile:
#   curl -fsSL https://raw.githubusercontent.com/lh1000/Mondschein-Daten/main/cloud/setup.sh | bash
# Kopie: tools/cloud/setup.sh im App-Repo (dort ändern, dann ins Daten-Repo kopieren).
set -euo pipefail

SDK=/opt/android-sdk
DATEN=/opt/mondschein/Daten
WERKZEUGE=commandlinetools-linux-16111833_latest.zip
RELEASES=https://github.com/lh1000/Mondschein-Daten/releases

android_sdk() {
    if [ ! -x "$SDK/cmdline-tools/latest/bin/sdkmanager" ]; then
        mkdir -p "$SDK/cmdline-tools"
        curl -fsSL -o /tmp/werkzeuge.zip "https://dl.google.com/android/repository/$WERKZEUGE"
        unzip -q -o /tmp/werkzeuge.zip -d /tmp/werkzeuge
        rm -rf "$SDK/cmdline-tools/latest"
        mv /tmp/werkzeuge/cmdline-tools "$SDK/cmdline-tools/latest"
        rm -rf /tmp/werkzeuge /tmp/werkzeuge.zip
    fi
    yes | "$SDK/cmdline-tools/latest/bin/sdkmanager" --sdk_root="$SDK" --licenses > /dev/null || true
    "$SDK/cmdline-tools/latest/bin/sdkmanager" --sdk_root="$SDK" \
        "platform-tools" "platforms;android-37.0" "build-tools;36.0.0" > /dev/null
    echo "Android-SDK fertig"
}

daten() {
    mkdir -p "$DATEN/licht" "$DATEN/wolken"
    # Liste des neuesten Datenstands; jede Datei steht dort mit ihrer Adresse
    curl -fsSL -o "$DATEN/daten.json" "$RELEASES/latest/download/daten.json"
    python3 - "$DATEN" <<'EOF'
import json, sys, urllib.request
from pathlib import Path
ordner = Path(sys.argv[1])
for d in json.loads((ordner / "daten.json").read_text())["dateien"]:
    if d["name"].endswith(".pmtiles"):
        continue  # Karten braucht nur das Handy
    ziel = ordner / d["name"]
    if not ziel.exists() or ziel.stat().st_size != d["groesse"]:
        urllib.request.urlretrieve(d["url"], ziel)
EOF
    for f in licht-hoehen.bin licht-kronenhoehe.bin licht-luecke.bin; do
        curl -fsSL -o "$DATEN/licht/$f" "$RELEASES/download/licht/$f" &
    done
    curl -fsSL -o "$DATEN/wolken/mondschein-wolken.bin" "$RELEASES/download/wolken/mondschein-wolken.bin" &
    wait
    echo "Daten fertig"
}

python_pakete() {
    python3 -m venv /opt/mondschein/venv
    /opt/mondschein/venv/bin/pip install -q --upgrade pip
    /opt/mondschein/venv/bin/pip install -q osmium numpy scipy rasterio shapely mapbox_vector_tile pmtiles
    echo "Python fertig"
}

mkdir -p /opt/mondschein
android_sdk &
daten &
python_pakete &
wait

# Umgebung für jede Shell der Sitzung (Gradle findet das SDK, die Tests die Daten)
cat > /etc/profile.d/mondschein.sh <<EOF
export ANDROID_HOME=$SDK
export ANDROID_SDK_ROOT=$SDK
export MONDSCHEIN_DATEN=$DATEN
export MONDSCHEIN_HOEHEN=$DATEN/mondschein-hoehen.bin
export MONDSCHEIN_LICHT=$DATEN/licht
export MONDSCHEIN_WOLKEN=$DATEN/wolken/mondschein-wolken.bin
export PATH=/opt/mondschein/venv/bin:\$PATH
EOF
grep -q mondschein.sh /root/.bashrc 2>/dev/null || echo '. /etc/profile.d/mondschein.sh' >> /root/.bashrc
echo "Mondschein-Umgebung fertig"
