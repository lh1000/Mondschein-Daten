"""Erzeugt Geländeschattierung und Höhenlinien für die Offline-Karte aus dem Höhenmodell.

Eingabe ist mondschein-hoehen.bin (tools/hoehen). Ausgabe sind zwei PMTiles-Dateien:

- mondschein-schummerung.pmtiles  Schräglicht-Schummerung als halbtransparente WebP-Bilder
                                  (Zoom 5–12, 512 px): Schatten dunkel, Sonnenhänge hell,
                                  ebene Flächen durchsichtig
- mondschein-hoehenlinien.pmtiles Höhenlinien als Vektorkacheln (Zoom 10–13, darüber vergrößert)

Die Schummerung wird hier berechnet statt von MapLibre auf dem Gerät: Dessen eingebaute
Schattierung bleibt selbst mit maximaler Stärke deutlich flacher als amtliche Reliefkarten.

Aufruf:
    python gelaende.py --hoehen <mondschein-hoehen.bin> --ausgabe <ordner> [--nur schummerung|hoehenlinien]
Für eine Region der Gebietserweiterung (Meilenstein 28) mit allen Höhenkacheln, die sie berührt:
    python gelaende.py --hoehen mondschein-hoehen-N47E009.bin … --gebiet 9.45,46.75,10.75,47.95
                       --region allgaeu-vorarlberg --ausgabe <ordner>
    → mondschein-schummerung-allgaeu-vorarlberg.pmtiles, mondschein-hoehenlinien-allgaeu-vorarlberg.pmtiles
"""

from __future__ import annotations

import argparse
import gzip
import io
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import mapbox_vector_tile
import numpy as np
from PIL import Image
from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import Writer
from scipy.ndimage import gaussian_filter, map_coordinates
from shapely.geometry import LineString, MultiLineString
from skimage.measure import find_contours

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hoehen"))
import hoehenmodell as hm  # noqa: E402  (gemeinsames Lesen der Höhendatei)

# Ausdehnung der Offline-Karte (tools/karte/README.md); für Regionen mit gebiet_setzen() geändert
WEST, SUED, OST, NORD = 10.6, 47.15, 13.4, 48.3


def gebiet_setzen(west: float, sued: float, ost: float, nord: float) -> None:
    """Kartenausschnitt für eine Region (Gebietserweiterung); gilt für alle Funktionen dieses Moduls."""
    global WEST, SUED, OST, NORD
    WEST, SUED, OST, NORD = west, sued, ost, nord

SCHUMMERUNG_ZOOM = range(5, 13)
SCHUMMERUNG_PX = 512  # Zoom 12 mit 512 px entspricht ca. 13 m je Pixel

# Schräglicht wie bei amtlichen Reliefkarten: Licht aus Nordwest, 45° über dem Horizont.
LICHT_AZIMUT = 315.0
LICHT_HOEHE = 45.0
UEBERHOEHUNG = 1.3  # verstärkt die Neigungen; mehr = plastischer
SCHATTEN_FARBE = (40, 33, 27)
SCHATTEN_DECKKRAFT = 0.75  # bei voll abgewandten Hängen
LICHT_DECKKRAFT = 0.65  # bei voll beleuchteten Hängen
LINIEN_ZOOM = range(10, 14)
# Zoom → (Abstand, Zählkurve) in Metern; ab Zoom 13 wie auf Wanderkarten 1:25 000.
AEQUIDISTANZ = {10: (200, 1000), 11: (100, 500), 12: (50, 250), 13: (20, 100)}

EXTENT = 4096
STICHPROBEN = 512  # Abtastpunkte je Kachelkante für die Höhenlinien
RAND = 16  # zusätzliche Abtastpunkte rundherum, damit Linien über die Kachelgrenze laufen
UEBERGANG = 30  # Bogensekunden (ca. 900 m), über die Kern- und Pufferebene am Kernrand überblendet werden


# ---------------------------------------------------------------- Höhenmodell lesen

def copernicus_ebene() -> hm.Ebene:
    """Copernicus GLO-30 (1″) für den Kartenausschnitt, einmalig als .npy zwischengespeichert."""
    gebiet = hm.Gebiet(sued=SUED, nord=NORD, west=WEST, ost=OST)
    nord, sued, west, ost = hm.raster(gebiet, 1)
    datei = hm.ARBEITSORDNER / "zeilen" / f"karte_copernicus_{nord}_{west}_{sued}_{ost}.npy"
    if not datei.exists():
        datei.parent.mkdir(parents=True, exist_ok=True)
        hoehen = hm.als_int16(hm.Mosaik(hm.ARBEITSORDNER / "copernicus").ausschnitt(nord, sued, west, ost))
        np.save(datei.with_name(datei.stem + "_teil.npy"), hoehen)
        datei.with_name(datei.stem + "_teil.npy").replace(datei)
    return hm.Ebene(nord, west, 1, np.load(datei, mmap_mode="r"))


class Hoehen:
    """Höhenmodell (Version 1 oder 2) zeilenweise eingeblendet; tastet bilinear ab."""

    def __init__(self, pfade: Path | list[Path]):
        # feinste zuerst: Kern 1/3″, dann Copernicus 1″ für den ganzen Kartenausschnitt, dann Puffer 3″.
        # Der Puffer ist ein Maximum je 90-m-Zelle für den Horizont; als Kartengrundlage wirkt er
        # beim Hineinzoomen treppig. Deshalb außerhalb des Kerns Copernicus in voller Auflösung.
        # Mehrere Dateien (Höhenkacheln der Regionen): alle Ebenen zusammen.
        pfade = [pfade] if isinstance(pfade, Path) else pfade
        alle = [e for p in pfade for e in hm.lies(p)] + [copernicus_ebene()]
        self.ebenen = sorted(alle, key=lambda e: e.schritt_bogensek)
        self.nachbarn = [nachbarseiten(e, alle) for e in self.ebenen]

    def abtasten(self, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
        """Höhen in Metern für Arrays in Grad."""
        ergebnis = None
        for e, nachbarn in zip(reversed(self.ebenen), reversed(self.nachbarn)):  # grob → fein
            zeilen, spalten = e.hoehen.shape
            z = (e.nord - lat * 3600 * e.teilung) / e.schritt
            s = (lon * 3600 * e.teilung - e.west) / e.schritt
            # nur den benötigten Ausschnitt lesen (die Ebene ist eingeblendet, nicht geladen)
            z0 = int(np.clip(np.floor(z.min()), 0, zeilen - 1))
            z1 = int(np.clip(np.ceil(z.max()) + 2, 1, zeilen))
            s0 = int(np.clip(np.floor(s.min()), 0, spalten - 1))
            s1 = int(np.clip(np.ceil(s.max()) + 2, 1, spalten))
            ausschnitt = e.meter(slice(z0, z1), slice(s0, s1))
            werte = map_coordinates(ausschnitt, [z - z0, s - s0], order=1, mode="nearest")
            if ergebnis is None:
                ergebnis = werte
                continue
            # Gewicht 1 im Inneren der feineren Ebene, linear auf 0 bis zu ihrem Rand; Ränder, an die eine
            # gleich feine Kachel anschließt, zählen nicht (sonst schimmerte dort Copernicus durch).
            seiten = [z, zeilen - 1 - z, s, spalten - 1 - s]  # Nord, Süd, West, Ost
            offen = [d for d, n in zip(seiten, nachbarn) if not n]
            abstand = (np.minimum.reduce(offen) if offen else np.full(z.shape, np.inf)) * e.schritt_bogensek
            gewicht = np.where(np.minimum.reduce(seiten) >= 0, np.clip(abstand / UEBERGANG, 0, 1), 0.0)
            gewicht[~np.isfinite(werte)] = 0
            ergebnis = gewicht * np.nan_to_num(werte) + (1 - gewicht) * ergebnis
        return ergebnis


def nachbarseiten(e: hm.Ebene, alle: list[hm.Ebene]) -> list[bool]:
    """Schließt an Nord-, Süd-, West- bzw. Ostrand von e eine gleich feine Ebene an?"""
    def rand(x):
        z, s = x.hoehen.shape
        return x.nord, x.nord - (z - 1) * x.schritt, x.west, x.west + (s - 1) * x.schritt  # N, S, W, O

    n, s, w, o = rand(e)
    gleich = [rand(f) for f in alle if f is not e and (f.schritt, f.teilung) == (e.schritt, e.teilung)]
    return [
        any(fs <= n < fn and fw < o and fo > w for fn, fs, fw, fo in gleich),
        any(fs < s <= fn and fw < o and fo > w for fn, fs, fw, fo in gleich),
        any(fw <= w < fo and fs < n and fn > s for fn, fs, fw, fo in gleich),
        any(fw < o <= fo and fs < n and fn > s for fn, fs, fw, fo in gleich),
    ]


# ---------------------------------------------------------------- Kachelgeometrie

def kacheln_im_gebiet(z: int) -> list[tuple[int, int]]:
    n = 2**z
    x0, x1 = int((WEST + 180) / 360 * n), int((OST + 180) / 360 * n)
    y0, y1 = merc_y(NORD, z), merc_y(SUED, z)
    return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def merc_y(lat: float, z: int) -> int:
    return int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * 2**z)


def gitter(z: int, x: int, y: int, n: int, rand: int) -> tuple[np.ndarray, np.ndarray]:
    """Breite/Länge der Pixelmitten einer Kachel mit n×n Pixeln plus Rand."""
    i = (np.arange(-rand, n + rand) + 0.5) / n
    lon = (x + i) / 2**z * 360 - 180
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * (y + i) / 2**z))))
    return np.meshgrid(lat, lon, indexing="ij")


# ---------------------------------------------------------------- Arbeiter

_hoehen: Hoehen | None = None


def _init(pfade: str, gebiet: tuple[float, float, float, float]) -> None:
    global _hoehen
    gebiet_setzen(*gebiet)
    _hoehen = Hoehen([Path(p) for p in pfade.split(os.pathsep)])


def beleuchtung(h: np.ndarray, meter_pro_px: float) -> np.ndarray:
    """Lambert-Beleuchtung je Pixel (0 = abgewandt, 1 = senkrecht beleuchtet); Zeilen laufen nach Süden."""
    d_sued, d_ost = np.gradient(h * UEBERHOEHUNG, meter_pro_px)
    # Flächennormale (Ost, Nord, Oben) = (−dz/dOst, −dz/dNord, 1) mit dz/dNord = −d_sued
    nx, ny, nz = -d_ost, d_sued, np.ones_like(h)
    laenge = np.sqrt(nx**2 + ny**2 + nz**2)
    a, b = math.radians(LICHT_AZIMUT), math.radians(LICHT_HOEHE)
    licht = (math.sin(a) * math.cos(b), math.cos(a) * math.cos(b), math.sin(b))
    return np.clip((nx * licht[0] + ny * licht[1] + nz * licht[2]) / laenge, 0, 1)


def schummerung_kachel(zxy: tuple[int, int, int]) -> tuple[tuple[int, int, int], bytes]:
    """Halbtransparentes Bild: dunkel, wo es dunkler als eine Ebene ist, hell, wo heller."""
    z, x, y = zxy
    lat, lon = gitter(z, x, y, SCHUMMERUNG_PX, 1)
    h = _hoehen.abtasten(lat, lon)
    meter_pro_px = 40_075_016 * math.cos(math.radians(float(lat.mean()))) / 2**z / SCHUMMERUNG_PX
    l = beleuchtung(h, meter_pro_px)[1:-1, 1:-1]
    eben = math.sin(math.radians(LICHT_HOEHE))  # Beleuchtung einer waagrechten Fläche
    schatten = np.clip((eben - l) / eben, 0, 1) * SCHATTEN_DECKKRAFT
    hell = np.clip((l - eben) / (1 - eben), 0, 1) * LICHT_DECKKRAFT
    rgba = np.zeros(l.shape + (4,), dtype=np.uint8)
    ist_schatten = schatten > hell
    rgba[ist_schatten, :3] = SCHATTEN_FARBE
    rgba[~ist_schatten, :3] = 255
    rgba[..., 3] = np.rint(np.maximum(schatten, hell) * 255)
    puffer = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(puffer, "WEBP", quality=75, alpha_quality=50, method=4)
    return zxy, puffer.getvalue()


def linien_kachel(zxy: tuple[int, int, int]) -> tuple[tuple[int, int, int], bytes | None]:
    z, x, y = zxy
    lat, lon = gitter(z, x, y, STICHPROBEN, RAND)
    h = _hoehen.abtasten(lat, lon)
    # Glätten gegen das Rauschen des Oberflächenmodells (Bäume, Gebäude): ca. 15 m im Gelände.
    meter_pro_px = 40_075_016 * math.cos(math.radians(float(lat.mean()))) / 2**z / STICHPROBEN
    h = gaussian_filter(h, sigma=max(0.7, 15 / meter_pro_px))

    abstand, zaehl = AEQUIDISTANZ[z]
    massstab = EXTENT / STICHPROBEN
    toleranz = 0.6 * massstab  # Vereinfachung um gut einen halben Abtastpunkt
    features = []
    for hoehe in range(int(math.ceil(h.min() / abstand)) * abstand, int(h.max()) + 1, abstand):
        linien = []
        for kontur in find_contours(h, hoehe):
            pkt = (kontur[:, ::-1] - RAND) * massstab  # (Zeile, Spalte) → (x, y) in Kachelkoordinaten
            if len(pkt) < 3:
                continue
            linie = LineString(pkt).simplify(toleranz)
            if linie.length >= 4 * massstab:
                linien.append(linie)
        if linien:
            features.append({
                "geometry": MultiLineString(linien),
                "properties": {"hoehe": hoehe, "index": int(hoehe % zaehl == 0)},
            })
    if not features:
        return zxy, None
    mvt = mapbox_vector_tile.encode(
        [{"name": "hoehenlinien", "features": features}],
        default_options={"extents": EXTENT, "y_coord_down": True},
    )
    return zxy, gzip.compress(mvt)


# ---------------------------------------------------------------- PMTiles schreiben

def schreibe(pfad: Path, kacheln: dict[int, bytes], typ: TileType, kompression: Compression, zooms: range,
             metadaten: dict) -> None:
    with open(pfad, "wb") as f:
        w = Writer(f)
        for tid in sorted(kacheln):
            w.write_tile(tid, kacheln[tid])
        w.finalize(
            {
                "tile_type": typ,
                "tile_compression": kompression,
                "min_zoom": zooms.start,
                "max_zoom": zooms.stop - 1,
                "min_lon_e7": int(WEST * 1e7),
                "min_lat_e7": int(SUED * 1e7),
                "max_lon_e7": int(OST * 1e7),
                "max_lat_e7": int(NORD * 1e7),
                "center_zoom": zooms.start,
                "center_lon_e7": int((WEST + OST) / 2 * 1e7),
                "center_lat_e7": int((SUED + NORD) / 2 * 1e7),
            },
            metadaten,
        )
    print(f"{pfad} geschrieben: {len(kacheln)} Kacheln, {pfad.stat().st_size / 1e6:.0f} MB")


def erzeuge(name: str, funktion, zooms: range, hoehen: list[Path], arbeiter: int) -> dict[int, bytes]:
    auftraege = [(z, x, y) for z in zooms for x, y in kacheln_im_gebiet(z)]
    print(f"{name}: {len(auftraege)} Kacheln")
    start = time.time()
    ergebnis = {}
    with ProcessPoolExecutor(arbeiter, initializer=_init,
                             initargs=(os.pathsep.join(map(str, hoehen)), (WEST, SUED, OST, NORD))) as pool:
        for n, (zxy, daten) in enumerate(pool.map(funktion, auftraege, chunksize=8), 1):
            if daten:
                ergebnis[zxy_to_tileid(*zxy)] = daten
            if n % 200 == 0:
                print(f"  {n}/{len(auftraege)} ({time.time() - start:.0f} s)")
    return ergebnis


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hoehen", type=Path, nargs="+", required=True)
    parser.add_argument("--ausgabe", type=Path, required=True)
    parser.add_argument("--arbeiter", type=int, default=3, help="parallele Prozesse (je ca. 300 MB Speicher)")
    parser.add_argument("--nur", choices=["schummerung", "hoehenlinien"])
    parser.add_argument("--gebiet", help="West,Süd,Ost,Nord in Grad (Standard: Kartengebiet des Kerns)")
    parser.add_argument("--region", help="Region der Gebietserweiterung: hängt -<region> an die Dateinamen")
    args = parser.parse_args()
    args.ausgabe.mkdir(parents=True, exist_ok=True)
    if args.gebiet:
        gebiet_setzen(*map(float, args.gebiet.split(",")))
    anhang = f"-{args.region}" if args.region else ""

    Hoehen(args.hoehen)  # Zwischendateien einmal anlegen, bevor die Arbeiter sie gleichzeitig öffnen
    lizenz = ("BEV ALS DTM (CC BY 4.0) · Bayerische Vermessungsverwaltung – www.geodaten.bayern.de (CC BY 4.0) · "
              "Copernicus DEM GLO-30 © DLR e.V. 2010–2014, © Airbus Defence and Space GmbH 2014–2018")
    if args.nur in (None, "schummerung"):
        bilder = erzeuge("Schummerung", schummerung_kachel, SCHUMMERUNG_ZOOM, args.hoehen, args.arbeiter)
        schreibe(args.ausgabe / f"mondschein-schummerung{anhang}.pmtiles", bilder, TileType.WEBP, Compression.NONE,
                 SCHUMMERUNG_ZOOM, {"name": "Mondschein Schummerung", "attribution": lizenz})
    if args.nur == "schummerung":
        return 0

    linien = erzeuge("Höhenlinien", linien_kachel, LINIEN_ZOOM, args.hoehen, args.arbeiter)
    schreibe(args.ausgabe / f"mondschein-hoehenlinien{anhang}.pmtiles", linien, TileType.MVT, Compression.GZIP, LINIEN_ZOOM,
             {"name": "Mondschein Höhenlinien", "attribution": lizenz,
              "vector_layers": [{"id": "hoehenlinien", "fields": {"hoehe": "Number", "index": "Number"}}]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
