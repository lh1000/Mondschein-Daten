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
import re
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
# Grunddaten: gehören zu keinem Gebiet, die App lädt sie immer (Übersichtskarte, Meilenstein 39)
GRUNDDATEIEN = {"mondschein-uebersicht.pmtiles": "Übersichtskarte aller Gebiete (OpenStreetMap, Protomaps)"}
# Ladbare Gebiete (Gebietserweiterung, docs/konzept.md Stufe 5): Die Dateien oben gehören zum Kerngebiet.
# Regionsdateien heißen mondschein-<art>-<region>.<endung>, Höhenkacheln mondschein-hoehen-N47E010.bin
# (gehören zu allen Regionen außer „kern“, die die Kachel berühren), die Horizontpuffer
# mondschein-hoehen-puffer-<gesamtgebiet>.bin (zu allen Regionen darin außer „kern“). Dateien ohne
# Zuordnung (z. B. der frühere gemeinsame Puffer) fallen aus der Liste. Dateien ohne Region wären Grunddaten.
GEBIETE = Path(__file__).resolve().parent.parent / "gebiete" / "gebiete.json"
KERN = {"id": "kern", "name": "Kerngebiet (Werdenfels bis Berchtesgaden)", "sued": 47.25, "nord": 47.90,
        "west": 10.70, "ost": 13.15}
ARTEN = {"karte": "Grundkarte", "wandern": "Wander-Ebene", "schummerung": "Schummerung",
         "hoehenlinien": "Höhenlinien", "wege": "Wegenetz", "wald": "Wald am Weg", "hoehen": "Höhen"}
REGIONSDATEI = re.compile(r"mondschein-(karte|wandern|schummerung|hoehenlinien|wege|wald|hoehen)-([A-Za-z0-9-]+)\.(bin|pmtiles)")
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


def regionen() -> list[dict]:
    """Regionen aus gebiete.json (Grenzen in Grad); ohne Datei nur das Kerngebiet."""
    if GEBIETE.exists():
        return json.loads(GEBIETE.read_text(encoding="utf-8"))["regionen"]
    return [KERN]


def gesamtgebiete() -> list[dict]:
    if not GEBIETE.exists():
        return []
    g = json.loads(GEBIETE.read_text(encoding="utf-8"))["gesamt"]
    return g if isinstance(g, list) else [g]


def zuordnung(name: str, alle: list[dict]) -> tuple[str, list[str]] | None:
    """Beschreibung und Regionen einer Datei; `None`, wenn sie nicht zu den Daten der App gehört."""
    if name in DATEIEN:
        return DATEIEN[name], ["kern"]
    if name in GRUNDDATEIEN:
        return GRUNDDATEIEN[name], []
    m = REGIONSDATEI.fullmatch(name)
    if not m:
        return None
    art, teil = m[1], m[2]
    weitere = [r for r in alle if r["id"] != "kern"]
    if art == "hoehen" and teil.startswith("puffer-"):
        # Horizontpuffer je Gesamtgebiet: gehört zu allen Regionen darin (außer „kern“)
        g = next((x for x in gesamtgebiete() if x.get("id") == teil.removeprefix("puffer-")), None)
        if g is None:
            return None
        zu = [r["id"] for r in weitere if r["sued"] < g["nord"] and r["nord"] > g["sued"] and r["west"] < g["ost"] and r["ost"] > g["west"]]
        return f"Horizont {g['name']} (Copernicus)", zu
    if art == "hoehen":
        k = re.fullmatch(r"N(\d\d)E(\d\d\d)", teil)
        if not k:
            return None
        b, l = int(k[1]), int(k[2])
        zu = [r["id"] for r in weitere if r["sued"] < b + 1 and r["nord"] > b and r["west"] < l + 1 and r["ost"] > l]
        return f"Höhen {teil} (amtliche Laserscan-Modelle, Copernicus)", zu
    region = next((r for r in alle if r["id"] == teil), None)
    if region is None:
        return None
    return f"{ARTEN[art]} {region['name']}", [teil]


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
    liste = {"stand": args.stand, "regionen": [], "dateien": []}
    alt = bisherige()
    alle = regionen()
    neu: list[str] = []
    # Erst die Dateien des Kerngebiets in fester Reihenfolge, dann die Regionsdateien (neu im Ordner
    # oder schon veröffentlicht)
    weitere = {p.name for p in args.daten.glob("mondschein-*") if zuordnung(p.name, alle)} | set(alt)
    namen = list(DATEIEN) + sorted(n for n in weitere if n not in DATEIEN and zuordnung(n, alle))
    for name in namen:
        beschreibung, zu = zuordnung(name, alle)
        pfad = args.daten / name
        if not pfad.exists():
            if name not in alt and name in OPTIONAL:
                print(f"  {name}: noch nie veröffentlicht – ausgelassen")
                continue
            if name not in alt:
                raise SystemExit(f"{pfad} fehlt und ist noch nie veröffentlicht worden")
            liste["dateien"].append({**alt[name], "beschreibung": beschreibung, "regionen": zu})
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
                   "sha256": summe, "url": url, "regionen": zu}
        liste["dateien"].append(eintrag)
        print(f" {eintrag['groesse'] / 1e6:.0f} MB" + (" – neu" if name in neu else " – unverändert"))
    benutzt = {r for d in liste["dateien"] for r in d["regionen"]}
    # Mit Grenzen: Gebiete „online“ (Meilenstein 32) braucht die App ohne geladene Kartendatei
    liste["regionen"] = [{k: r[k] for k in ("id", "name", "sued", "nord", "west", "ost") if k in r}
                         for r in alle if r["id"] in benutzt]
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
