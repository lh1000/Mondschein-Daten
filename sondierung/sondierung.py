"""Sondierung der Datenquellen für die Gebietserweiterung (Mondschein, Meilenstein 27 / E2).

Fragt je Quelle einen kleinen Ausschnitt ab (HTTP-Range) und misst Antwort und Durchsatz, damit lange
Aufbereitungsläufe nicht an einer Sperre oder Drosselung scheitern. Ändert nichts.
"""

import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

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


def verzeichnis(name: str, url: str, zeilen: int = 40) -> str:
    """Verzeichnisliste (HTML) als Text: Einträge mit Datum, gekürzt."""
    daten = pruefe(name, url, 1 << 20).decode("utf-8", "replace")
    eintraege = re.findall(r'<a href="([^"]+)">[^<]*</a>\s*([^<\n]*)', daten)
    for href, rest in eintraege[:zeilen]:
        print(f"     {href:60s} {rest.strip()}")
    if len(eintraege) > zeilen:
        print(f"     … {len(eintraege) - zeilen} weitere")
    return daten


def dwd() -> None:
    """DWD-Umstellung prüfen (Hinweis: alter Pfad icon-d2/grib endet nach dem 30.11.2026?)."""
    alt = "https://opendata.dwd.de/weather/nwp/icon-d2/grib/"
    verzeichnis("DWD weather/nwp", "https://opendata.dwd.de/weather/nwp/")
    liste = verzeichnis("DWD icon-d2/grib (alt)", alt)
    laeufe = sorted(set(re.findall(r'href="(\d\d)/"', liste)))
    if laeufe:
        verzeichnis(f"DWD alt Lauf {laeufe[-1]}", f"{alt}{laeufe[-1]}/")
        verzeichnis(f"DWD alt clc {laeufe[-1]}", f"{alt}{laeufe[-1]}/clc/", 8)
    v1 = verzeichnis("DWD v1", "https://opendata.dwd.de/weather/nwp/v1/")
    if v1:
        verzeichnis("DWD v1/m", "https://opendata.dwd.de/weather/nwp/v1/m/")
        d2 = verzeichnis("DWD v1/m/icon-d2", "https://opendata.dwd.de/weather/nwp/v1/m/icon-d2/")
        unter = re.findall(r'href="([^"./][^"]*/)"', d2)
        for u in unter[:3]:
            verzeichnis(f"DWD v1/m/icon-d2/{u}", f"https://opendata.dwd.de/weather/nwp/v1/m/icon-d2/{u}", 30)
    # Neuer Pfad tiefer: CLC bis zu den Dateien (Gitterarten?)
    pfad = "https://opendata.dwd.de/weather/nwp/v1/m/icon-d2/p/CLC/"
    for _ in range(6):
        liste = verzeichnis(f"DWD {pfad[37:]}", pfad, 12)
        unter = [u for u in re.findall(r'href="([^"./][^"]*/)"', liste)]
        if not unter:
            break
        pfad += unter[-1]
    # Alte Dateien: gibt es noch das regelmäßige Gitter?
    if laeufe:
        liste = pruefe("DWD alt clc (ganz)", f"{alt}{laeufe[-1]}/clc/", 4 << 20).decode("utf-8", "replace")
        print("     regular-lat-lon:", liste.count("regular-lat-lon"), " icosahedral:", liste.count("icosahedral"))
    # Newsletter (RSS): Einträge zu ICON-D2, Abschaltung, Umstellung
    rss = pruefe("DWD Newsletter RSS", "https://www.dwd.de/SiteGlobals/Functions/RSSFeed/DE/opendata/opendata_lsb.xml", 4 << 20).decode("utf-8", "replace")
    for titel, text in re.findall(r"<item>.*?<title>(.*?)</title>.*?<description>(.*?)</description>", rss, re.S)[:25]:
        print("  RSS:", titel.strip()[:150])
        t = re.sub(r"<[^>]+>|&lt;[^&]*&gt;", " ", text)
        if re.search(r"icon-d2|ICON-D2|regular|abgeschaltet|eingestellt|v1|30\.11|November", t):
            print("       ", re.sub(r"\s+", " ", t)[:1200])
    # Hinweise/Ankündigungen im Wurzelverzeichnis
    wurzel = verzeichnis("DWD Wurzel", "https://opendata.dwd.de/", 60)
    for datei in re.findall(r'href="([^"]*(?:README|readme|news|NEWS|Hinweis|hinweis)[^"]*)"', wurzel):
        print(pruefe(f"DWD {datei}", f"https://opendata.dwd.de/{datei}", 1 << 16).decode("utf-8", "replace")[:3000])


def dwd2() -> None:
    """Neuer DWD-Pfad (v1): Dateinamen bis hinunter, eine CLC- und eine HHL-Datei laden und ihr Gitter zeigen."""
    import bz2
    v1 = "https://opendata.dwd.de/weather/nwp/v1/m/icon-d2/p/"
    def tiefer(pfad: str, schritte: int = 12) -> str | None:
        for _ in range(schritte):
            liste = verzeichnis(f"DWD {pfad[37:]}", pfad, 8)
            dateien = re.findall(r'href="([^"/]+\.grib2(?:\.bz2)?)"', liste)
            if dateien:
                return pfad + dateien[0]
            unter = re.findall(r'href="([^"./][^"]*/)"', liste)
            if not unter:
                return None
            pfad += unter[-1] if "r/" in pfad[-3:] or "%3A" in unter[-1] else unter[0]
        return None
    for param in ("CLC", "HHL", "CLAT", "CLON"):
        url = tiefer(v1 + param + "/")
        print(f"     → {param}: {url}")
        if not url:
            continue
        daten = pruefe(f"DWD {param} Datei", url, 64 << 20)
        if url.endswith(".bz2"):
            daten = bz2.decompress(daten)
        try:
            import eccodes
            g = eccodes.codes_new_from_message(daten)
            for k in ("gridType", "numberOfDataPoints", "Ni", "Nj", "typeOfLevel", "level", "shortName", "numberOfGridUsed", "uuidOfHGrid",
                      "latitudeOfFirstGridPointInDegrees", "longitudeOfFirstGridPointInDegrees", "iDirectionIncrementInDegrees"):
                try:
                    print(f"       {k} = {eccodes.codes_get(g, k)}")
                except Exception as fehler:  # noqa: BLE001
                    print(f"       {k}: {fehler}")
            eccodes.codes_release(g)
        except Exception as fehler:  # noqa: BLE001
            print(f"     eccodes: {fehler}")


def links(name: str, url: str, muster: str = "", zeilen: int = 40) -> str:
    """Seite holen und Verweise (href/src/url-artige Zeichenketten) ausgeben, die [muster] enthalten."""
    text = pruefe(name, url, 4 << 20).decode("utf-8", "replace")
    gefunden = sorted(set(re.findall(r'(?:href|src)="([^"]+)"', text) + re.findall(r'https?://[^\s"<>\']+', text)))
    treffer = [g for g in gefunden if muster.lower() in g.lower()]
    for g in treffer[:zeilen]:
        print(f"     {g}")
    if len(treffer) > zeilen:
        print(f"     … {len(treffer) - zeilen} weitere")
    if not treffer:
        print("     (keine Verweise) Anfang: " + re.sub(r"\s+", " ", text[:600]))
    return text


def hoehen2() -> None:
    """Amtliche Höhenquellen für Meilenstein 40: Baden-Württemberg, Elsass, Niederlande, Niedersachsen."""
    print("--- LGL Baden-Württemberg")
    links("LGL Startseite", "https://opengeodata.lgl-bw.de/", "")
    for pfad in ("data/", "data/dgm/", "data/dgm1/"):
        links(f"LGL {pfad}", f"https://opengeodata.lgl-bw.de/{pfad}", "", 15)
    links("LGL WCS DGM1", "https://owsproxy.lgl-bw.de/owsproxy/ows/WCS_INSP_BW_Hoehe_Coverage_DGM1?SERVICE=WCS&REQUEST=GetCapabilities", "", 10)
    print("--- IGN Géoplateforme")
    cap = pruefe("IGN WMS-R Capabilities", "https://data.geopf.fr/wms-r?SERVICE=WMS&REQUEST=GetCapabilities&VERSION=1.3.0", 30 << 20).decode("utf-8", "replace")
    print("     Formate: " + ", ".join(sorted(set(re.findall(r"<Format>([^<]+)</Format>", cap)))[:30]))
    for n in sorted(set(re.findall(r"<Name>([^<]*(?:ELEVATION|MNT|ALTI)[^<]*)</Name>", cap)))[:40]:
        print(f"     Ebene {n}")
    for fmt in ("image/geotiff", "image/x-bil;bits=32"):
        url = ("https://data.geopf.fr/wms-r?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS=ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES"
               f"&STYLES=&CRS=EPSG:4326&BBOX=48.0,7.3,48.01,7.31&WIDTH=100&HEIGHT=100&FORMAT={urllib.parse.quote(fmt)}")
        d = pruefe(f"IGN GetMap {fmt}", url, 1 << 20)
        print(f"     Anfang {d[:16]!r}, {len(d)} Byte")
    links("IGN Téléchargement LiDAR HD MNT", "https://data.geopf.fr/telechargement/resource/LIDARHD-MNT", "", 10)
    print("--- PDOK AHN")
    cap = pruefe("PDOK AHN WCS Capabilities", "https://service.pdok.nl/rws/ahn/wcs/v1_0?SERVICE=WCS&REQUEST=GetCapabilities&VERSION=2.0.1", 4 << 20).decode("utf-8", "replace")
    print("     Coverages: " + ", ".join(sorted(set(re.findall(r"CoverageId>([^<]+)<", cap)))))
    print("     Formate: " + ", ".join(sorted(set(re.findall(r"formatSupported>([^<]+)<", cap)))))
    url = ("https://service.pdok.nl/rws/ahn/wcs/v1_0?SERVICE=WCS&VERSION=2.0.1&REQUEST=GetCoverage&COVERAGEID=dtm_05m"
           "&FORMAT=image/tiff&SUBSET=x(120000,120500)&SUBSET=y(487000,487500)")
    d = pruefe("PDOK GetCoverage dtm_05m 500 m", url, 8 << 20)
    print(f"     Anfang {d[:8]!r}, {len(d)} Byte")
    links("PDOK Atom dtm_05m", "https://service.pdok.nl/rws/ahn/atom/dtm_05m.xml", ".tif", 5)
    print("--- LGLN Niedersachsen")
    links("LGLN OpenGeoData", "https://opengeodata.lgln.niedersachsen.de/", "", 40)
    for u in ("https://dgm.stac.lgln.niedersachsen.de/", "https://stac.lgln.niedersachsen.de/", "https://opengeodata.lgln.niedersachsen.de/stac/"):
        links(f"LGLN STAC? {u}", u, "", 15)
    links("LGLN Hub", "https://ni-lgln-opengeodata.hub.arcgis.com/", "dgm", 20)


def hoehen3() -> None:
    """Zweite Runde Meilenstein 40: Dateischema LGL BW, LGLN-STAC, PDOK-Skalierung, IGN-Bildgröße."""
    print("--- LGL: Verweise im Programmcode der Seite")
    seite = pruefe("LGL Startseite", "https://opengeodata.lgl-bw.de/", 1 << 20).decode("utf-8", "replace")
    for js in sorted(set(re.findall(r'(?:src|href)="([^"]+\.js)"', seite))):
        text = pruefe(f"LGL {js}", f"https://opengeodata.lgl-bw.de/{js}", 8 << 20).decode("utf-8", "replace")
        for m in sorted(set(re.findall(r'["`\'][^"`\'\s]{0,80}(?:dgm|/data/|\.zip|\.tif|json)[^"`\'\s]{0,80}["`\']', text, re.I)))[:40]:
            print(f"     {m}")
    for u in ("https://opengeodata.lgl-bw.de/data/dgm/dgm1_32_400_5300_2_bw.zip",
              "https://opengeodata.lgl-bw.de/data/dgm/index.json", "https://opengeodata.lgl-bw.de/assets/config.json"):
        d = pruefe(f"LGL {u}", u, 1 << 16)
        print(f"     Anfang {d[:120]!r}")
    print("--- LGLN STAC")
    links("LGLN Collections", "https://dgm.stac.lgln.niedersachsen.de/collections", "collections/", 20)
    such = pruefe("LGLN Suche", "https://dgm.stac.lgln.niedersachsen.de/search?bbox=8.5,53.5,8.52,53.52&limit=3", 2 << 20).decode("utf-8", "replace")
    try:
        for f in json.loads(such)["features"]:
            print(f"     {f['id']} {f.get('collection')}: " + ", ".join(f"{k}={a['href']}" for k, a in f["assets"].items()))
    except Exception as fehler:  # noqa: BLE001
        print(f"     {fehler}: {such[:500]}")
    print("--- PDOK Skalierung")
    for zusatz in ("&SCALEFACTOR=0.1", "&SCALESIZE=x(500),y(500)", "&SCALEAXES=x(0.1),y(0.1)"):
        url = ("https://service.pdok.nl/rws/ahn/wcs/v1_0?SERVICE=WCS&VERSION=2.0.1&REQUEST=GetCoverage&COVERAGEID=dtm_05m"
               "&FORMAT=image/tiff&SUBSET=x(120000,125000)&SUBSET=y(487000,492000)" + zusatz)
        d = pruefe(f"PDOK 5 km{zusatz}", url, 64 << 20)
        print(f"     {len(d)} Byte, Anfang {d[:8]!r}")
    print("--- IGN Bildgröße")
    for n in (2048, 4096):
        url = ("https://data.geopf.fr/wms-r?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS=ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES"
               f"&STYLES=&CRS=EPSG:4326&BBOX=48.0,7.3,48.1,7.4&WIDTH={n}&HEIGHT={n}&FORMAT=image/geotiff")
        d = pruefe(f"IGN {n}²", url, 80 << 20)
        print(f"     {len(d)} Byte, Anfang {d[:8]!r}")


def hoehen4() -> None:
    """Dritte Runde Meilenstein 40: LGL-Produktliste, Lückenwerte bei IGN und PDOK."""
    for pfad in ("assets/config/local/odp-products.json", "assets/environment/config.json"):
        text = pruefe(f"LGL {pfad}", f"https://opengeodata.lgl-bw.de/{pfad}", 4 << 20).decode("utf-8", "replace")
        for m in re.finditer(r"dgm", text, re.I):
            print("     …" + re.sub(r"\s+", " ", text[max(0, m.start() - 300):m.start() + 400]) + "…")
            break
        for u in sorted(set(re.findall(r'https?://[^"\s]+', text)))[:30]:
            print(f"     {u}")
    import numpy as np
    from rasterio.io import MemoryFile
    for name, b, l in (("Freiburg (DE)", 48.0, 7.85), ("Colmar (FR)", 48.08, 7.35), ("Rhein", 48.2, 7.6)):
        url = ("https://data.geopf.fr/wms-r?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS=ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES"
               f"&STYLES=&CRS=EPSG:4326&BBOX={b},{l},{b + 0.01},{l + 0.01}&WIDTH=50&HEIGHT=50&FORMAT=image/geotiff")
        with MemoryFile(pruefe(f"IGN {name}", url, 1 << 20)) as m, m.open() as d:
            a = d.read(1)
            print(f"     dtype {a.dtype}, nodata {d.nodata}, crs {d.crs}, min {a.min()}, max {a.max()}, Werte {np.unique(a)[:5]}")
    url = ("https://service.pdok.nl/rws/ahn/wcs/v1_0?SERVICE=WCS&VERSION=2.0.1&REQUEST=GetCoverage&COVERAGEID=dtm_05m"
           "&FORMAT=image/tiff&SUBSET=x(120000,125000)&SUBSET=y(487000,492000)&SCALESIZE=x(500),y(500)")
    with MemoryFile(pruefe("PDOK", url, 8 << 20)) as m, m.open() as d:
        a = d.read(1)
        print(f"     dtype {a.dtype}, nodata {d.nodata}, crs {d.crs}, transform {d.transform}, Anteil Lücken {(a == d.nodata).mean() if d.nodata is not None else np.isnan(a).mean():.2f}")
    url = "https://dgm1.s3.eu-de.cloud-object-storage.appdomain.cloud/L2002/Vollkacheln/dgm1_32_468_5930_1_ni_2020.tif"
    with MemoryFile(pruefe("LGLN Kachel", url, 32 << 20)) as m, m.open() as d:
        print(f"     {d.width}×{d.height}, dtype {d.dtypes[0]}, nodata {d.nodata}, crs {d.crs}, Übersichten {d.overviews(1)}, Blöcke {d.block_shapes}")


def hoehen5() -> None:
    """Vierte Runde: Eintrag DGM1 der LGL-Produktliste vollständig, eine Kachel versuchen."""
    text = pruefe("LGL Produkte", "https://opengeodata.lgl-bw.de/assets/config/local/odp-products.json", 4 << 20).decode("utf-8")
    produkte = json.loads(text)
    def finde(x):
        if isinstance(x, dict):
            if x.get("uuid") == "dgm1":
                return x
            for v in x.values():
                r = finde(v)
                if r:
                    return r
        if isinstance(x, list):
            for v in x:
                r = finde(v)
                if r:
                    return r
    print(json.dumps(finde(produkte), indent=1, ensure_ascii=False)[:6000])


def main() -> None:
    import sys
    if "hoehen5" in sys.argv[1:]:
        hoehen5()
        return
    if "hoehen4" in sys.argv[1:]:
        hoehen4()
        return
    if "hoehen3" in sys.argv[1:]:
        hoehen3()
        return
    if "hoehen2" in sys.argv[1:]:
        hoehen2()
        return
    if "vergleich" in sys.argv[1:]:
        import subprocess
        subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "dwd_vergleich.py")], check=True)
        return
    if "dwd2" in sys.argv[1:]:
        dwd2()
        return
    if "dwd" in sys.argv[1:]:
        dwd()
        return
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
    # Alle Geofabrik-Extrakte der Regionen (nur der Anfang jeder Datei)
    gebiete = json.loads((Path(__file__).resolve().parent.parent / "gebiete" / "gebiete.json").read_text(encoding="utf-8"))
    for pfad in sorted({o for r in gebiete["regionen"] for o in r.get("osm", [])}):
        pruefe(f"Geofabrik {pfad}", f"https://download.geofabrik.de/{pfad}-latest.osm.pbf", 1 << 16)


if __name__ == "__main__":
    main()
