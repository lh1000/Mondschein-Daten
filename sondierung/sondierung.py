"""Sondierung der Datenquellen für die Gebietserweiterung (Mondschein, Meilenstein 27 / E2).

Fragt je Quelle einen kleinen Ausschnitt ab (HTTP-Range) und misst Antwort und Durchsatz, damit lange
Aufbereitungsläufe nicht an einer Sperre oder Drosselung scheitern. Ändert nichts.
"""

import json
import re
import time
import urllib.request

AGENT = {"User-Agent": "Mondschein-Daten (github.com/lh1000/Mondschein-Daten)"}
BYTES = 20 << 20


def holen(url: str, bytes_: int = BYTES) -> tuple[int, int, float, bytes]:
    anfrage = urllib.request.Request(url, headers={**AGENT, "Range": f"bytes=0-{bytes_ - 1}"})
    beginn = time.time()
    with urllib.request.urlopen(anfrage, timeout=120) as antwort:
        daten = antwort.read(bytes_)
        return antwort.status, len(daten), time.time() - beginn, daten


def pruefe(name: str, url: str, bytes_: int = BYTES) -> bytes:
    try:
        status, n, dauer, daten = holen(url, bytes_)
        print(f"OK   {name}: HTTP {status}, {n / 1e6:.1f} MB in {dauer:.1f} s = {n / 1e6 / max(dauer, 1e-3):.1f} MB/s")
        return daten
    except Exception as fehler:  # noqa: BLE001
        print(f"FEHL {name}: {fehler}  ({url})")
        return b""


def main() -> None:
    pruefe("BEV ALS DTM (COG)", "https://data.bev.gv.at/download/ALS/DTM/20250915/ALS_DTM_CRS3035RES50000mN2650000E4400000.tif")
    meta = pruefe("Bayern DGM1 Metalink", "https://geodaten.bayern.de/odd/a/dgm/dgm1/meta/metalink/09.meta4", 2 << 20)
    kachel = re.search(rb'<file name="(\d+_\d+\.tif)">', meta)
    if kachel:
        name = kachel[1].decode()
        pruefe("Bayern DGM1 Kachel (download1)", f"https://download1.bayernwolke.de/a/dgm/dgm1/{name}")
        pruefe("Bayern DGM1 Kachel (download2)", f"https://download2.bayernwolke.de/a/dgm/dgm1/{name}")
    pruefe("LGL BW OpenGeoData", "https://opengeodata.lgl-bw.de/", 1 << 20)
    stac = pruefe("swisstopo STAC swissALTI3D", "https://data.geo.admin.ch/api/stac/v0.9/collections/ch.swisstopo.swissalti3d/items?limit=1", 1 << 20)
    try:
        assets = json.loads(stac)["features"][0]["assets"]
        href = next(a["href"] for a in assets.values() if a["href"].endswith(".tif") and "_2_" in a["href"])
        pruefe("swissALTI3D 2 m Kachel", href)
    except Exception as fehler:  # noqa: BLE001
        print(f"FEHL swissALTI3D Kachel aus STAC: {fehler}")
    pruefe("IGN Géoplateforme (RGE ALTI)", "https://data.geopf.fr/telechargement/resource/RGEALTI", 1 << 20)
    pruefe("Copernicus DEM (S3)", "https://copernicus-dem-30m.s3.amazonaws.com/Copernicus_DSM_COG_10_N47_00_E010_00_DEM/Copernicus_DSM_COG_10_N47_00_E010_00_DEM.tif")
    pruefe("Geofabrik", "https://download.geofabrik.de/europe/germany/baden-wuerttemberg-latest.osm.pbf")
    pruefe("Protomaps Builds", "https://build-metadata.protomaps.dev/builds.json", 1 << 20)
    pruefe("Meta CHMv2 (S3)", "https://dataforgood-fb-data.s3.amazonaws.com/forests/v2/global/dinov3_global_chm_v2_ml3/chm/1202300212.tif")
    pruefe("DWD Open Data ICON-D2", "https://opendata.dwd.de/weather/nwp/icon-d2/grib/", 1 << 20)


if __name__ == "__main__":
    main()
