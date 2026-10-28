"""Veröffentlicht die Datendateien der App als Release im öffentlichen Repo lh1000/Mondschein-Daten.

Die App lädt die Liste daten.json des neuesten Release
(https://github.com/lh1000/Mondschein-Daten/releases/latest/download/daten.json) und daraus nur
die Dateien, deren Prüfsumme sich geändert hat (docs/konzept.md, Meilenstein 9).

Hochgeladen werden nur neue und geänderte Dateien. Für unveränderte (gleiche Prüfsumme wie in der
daten.json des bisher neuesten Release) zeigt die neue Liste auf die Datei im älteren Release. Fehlt
eine Datei im Ordner, bleibt ihr bisheriger Eintrag (so veröffentlicht GitHub Actions im Daten-Repo
nur das Wegenetz, ohne Karte und Höhenmodell zu laden).

Aufruf:
    python veroeffentlichen.py --daten ..\\..\\..\\Daten [--stand 2026-10-25] [--nur-liste]

Braucht das GitHub-Werkzeug gh (angemeldet). Beim ersten Mal ca. 1 GB Upload, danach nur geänderte Dateien.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = "lh1000/Mondschein-Daten"
GH = shutil.which("gh") or r"C:\Program Files\GitHub CLI\gh.exe"

# Name → Beschreibung; Reihenfolge = Reihenfolge beim Herunterladen (Wichtigstes zuerst)
DATEIEN = {
    "mondschein.pmtiles": "Grundkarte (OpenStreetMap, Protomaps)",
    "mondschein-wandern.pmtiles": "Wander-Ebene (OpenStreetMap)",
    "mondschein-hoehen.bin": "Höhenmodell (BEV, Bayern, Copernicus)",
    "mondschein-schummerung.pmtiles": "Schummerung",
    "mondschein-hoehenlinien.pmtiles": "Höhenlinien",
    "mondschein-wege.bin": "Wegenetz (OpenStreetMap)",
    "mondschein-wald.bin": "Wald am Weg (Meta/WRI CHMv2, Copernicus)",
}
# Ladbare Gebiete (Gebietserweiterung E1, notizen/recherche/gebietserweiterung.md). Die Dateien oben
# gehören zum Kerngebiet; Dateien ohne Region (leere Liste) wären Grunddaten, die jede App lädt.
REGIONEN = [{"id": "kern", "name": "Kerngebiet (Werdenfels bis Berchtesgaden)"}]
REGION_DER_DATEI = {name: ["kern"] for name in DATEIEN}
# Dürfen fehlen, auch wenn sie noch nie veröffentlicht wurden (eigener Workflow im Daten-Repo)
OPTIONAL = {"mondschein-wald.bin"}
LISTE = f"https://github.com/{REPO}/releases/latest/download/daten.json"


def bisherige() -> dict[str, dict]:
    """Einträge der daten.json im neuesten Release (Name → Eintrag); leer, wenn es keine gibt."""
    try:
        with urllib.request.urlopen(LISTE, timeout=60) as antwort:
            return {d["name"]: d for d in json.load(antwort)["dateien"]}
    except Exception as fehler:  # noqa: BLE001 – erstes Release oder kein Netz: alles hochladen
        print(f"  keine bisherige daten.json ({fehler}) – lade alle Dateien hoch")
        return {}


def pruefsumme(pfad: Path) -> str:
    h = hashlib.sha256()
    with open(pfad, "rb") as f:
        for block in iter(lambda: f.read(8 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--daten", type=Path, required=True)
    parser.add_argument("--stand", default=dt.date.today().isoformat())
    parser.add_argument("--nur-liste", action="store_true", help="nur daten.json schreiben, nichts hochladen")
    args = parser.parse_args()

    tag = f"daten-{args.stand}"
    liste = {"stand": args.stand, "regionen": REGIONEN, "dateien": []}
    alt = bisherige()
    neu: list[str] = []
    for name, beschreibung in DATEIEN.items():
        pfad = args.daten / name
        if not pfad.exists():
            if name not in alt and name in OPTIONAL:
                print(f"  {name}: noch nie veröffentlicht – ausgelassen")
                continue
            if name not in alt:
                raise SystemExit(f"{pfad} fehlt und ist noch nie veröffentlicht worden")
            liste["dateien"].append({**alt[name], "regionen": REGION_DER_DATEI[name]})
            print(f"  {name}: nicht im Ordner – bisheriger Eintrag bleibt")
            continue
        print(f"  {name}: Prüfsumme …", end="", flush=True)
        summe = pruefsumme(pfad)
        vorher = alt.get(name)
        if vorher is not None and vorher["sha256"] == summe:
            url = vorher["url"]  # unverändert: Datei im älteren Release
        else:
            url = f"https://github.com/{REPO}/releases/download/{tag}/{name}"
            neu.append(name)
        eintrag = {"name": name, "beschreibung": beschreibung, "groesse": pfad.stat().st_size,
                   "sha256": summe, "url": url, "regionen": REGION_DER_DATEI[name]}
        liste["dateien"].append(eintrag)
        print(f" {eintrag['groesse'] / 1e6:.0f} MB" + (" – neu" if name in neu else " – unverändert"))
    ausgabe = args.daten / "daten.json"
    ausgabe.write_text(json.dumps(liste, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{ausgabe} geschrieben")
    if args.nur_liste:
        return 0

    notizen = (f"Datenstand {args.stand}. Quellen und Lizenzen: siehe README. "
               "Die App Mondschein lädt diese Dateien über daten.json.")
    gibt_es = subprocess.run([GH, "release", "view", tag, "--repo", REPO], capture_output=True).returncode == 0
    if gibt_es:
        # Zweiter Lauf am selben Tag: Dateien im bestehenden Release ersetzen – erst die Daten, dann die
        # Liste, damit daten.json nie auf eine Datei zeigt, die es noch nicht gibt
        if neu:
            subprocess.run([GH, "release", "upload", tag, *[str(args.daten / n) for n in neu], "--repo", REPO,
                            "--clobber"], check=True)
        subprocess.run([GH, "release", "upload", tag, str(ausgabe), "--repo", REPO, "--clobber"], check=True)
        subprocess.run([GH, "release", "edit", tag, "--repo", REPO, "--latest"], check=True)
        print(f"Release {tag} aktualisiert")
    else:
        dateien = [str(ausgabe)] + [str(args.daten / n) for n in neu]
        subprocess.run([GH, "release", "create", tag, *dateien, "--repo", REPO, "--title", f"Daten {args.stand}",
                        "--notes", notizen, "--latest"], check=True)
        print(f"Release {tag} veröffentlicht")
    return 0


if __name__ == "__main__":
    sys.exit(main())
