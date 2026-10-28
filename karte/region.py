"""Hilfen für die Regions-Workflows (Mondschein, Gebietserweiterung): Gebiet, OSM-Extrakte und
Höhenkacheln einer Region aus gebiete/gebiete.json bzw. der daten.json des neuesten Release.

    python karte/region.py gebiet <region> [rand]   → West,Süd,Ost,Nord (mit Rand in Grad)
    python karte/region.py osm <region>             → Geofabrik-URLs, eine je Zeile
    python karte/region.py hoehen <region>          → URLs der Höhenkacheln der Region, eine je Zeile
"""

import json
import sys
import urllib.request
from pathlib import Path

GEBIETE = Path(__file__).resolve().parent.parent / "gebiete" / "gebiete.json"
LISTE = "https://github.com/lh1000/Mondschein-Daten/releases/latest/download/daten.json"


def region(rid: str) -> dict:
    for r in json.loads(GEBIETE.read_text(encoding="utf-8"))["regionen"]:
        if r["id"] == rid:
            return r
    sys.exit(f"Region {rid} fehlt in {GEBIETE}")


def main() -> None:
    was, rid = sys.argv[1], sys.argv[2]
    r = region(rid)
    if was == "gebiet":
        rand = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
        print(f"{r['west'] - rand:.4f},{r['sued'] - rand:.4f},{r['ost'] + rand:.4f},{r['nord'] + rand:.4f}")
    elif was == "osm":
        for pfad in r["osm"]:
            print(f"https://download.geofabrik.de/{pfad}-latest.osm.pbf")
    elif was == "hoehen":
        with urllib.request.urlopen(f"{LISTE}?t=1", timeout=60) as antwort:
            for d in json.load(antwort)["dateien"]:
                if d["name"].startswith("mondschein-hoehen-N") and rid in d.get("regionen", []):
                    print(d["url"])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
