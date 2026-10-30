"""Amtliche Laserscan-Geländemodelle (1 m) für das Höhenmodell der App.

Quellen, alle Airborne Laserscanning (BEV und Bayern CC BY 4.0, swisstopo Open Data mit Quellenangabe):

- Österreich: BEV ALS DTM, 50-km-Kacheln in EPSG:3035 als Cloud-Optimized GeoTIFF (COG). Ein COG
  enthält verkleinerte Übersichtsstufen und lässt sich blockweise per HTTP lesen – geladen wird nur
  der benötigte Ausschnitt in der benötigten Auflösung (Kern 2 m, Puffer 8 m).
- Bayern: DGM1 der Bayerischen Vermessungsverwaltung, 1-km-Kacheln in EPSG:25832 (kein COG). Jede
  Kachel wird in den Arbeitsspeicher geladen, verrechnet und verworfen – es bleibt nichts liegen.
- Schweiz und Liechtenstein: swissALTI3D 2 m (swisstopo), 1-km-Kacheln in EPSG:2056, Liste über die
  STAC-Schnittstelle (Gebietserweiterung, Meilenstein 31).

Umrechnung auf das geographische Zielraster: Jedes Quellpixel zählt in der Zielzelle, in der seine
Mitte liegt. Je Zelle werden Summe und Anzahl der Pixel über alle Kacheln aufaddiert; Mittelwert =
Summe / Anzahl. So stimmen auch Zellen auf Kachel- und Staatsgrenzen. (GDALs Umprojektion mit
„sum“ ist hier rund 100-mal langsamer und zählt in GDAL 3.10 nodata-Pixel mit.)
Die Summen liegen als Dateien auf der Festplatte (np.memmap) und überstehen einen Abbruch: Erledigte
Kacheln stehen in einer Liste und werden beim nächsten Lauf übersprungen.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
import rasterio.errors
import rasterio.transform
import rasterio.warp
from affine import Affine
from numpy.lib.format import open_memmap
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.io import MemoryFile
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds

ETRS89 = CRS.from_epsg(4258)  # geographisch; Unterschied zu WGS84 im Gebiet < 1 m
LAEA = CRS.from_epsg(3035)  # BEV
UTM32 = CRS.from_epsg(25832)  # Bayern
QUELL_LUECKE = -9999.0
KEINE_DATEN = -32768  # Lücke in der Höhendatei (wie hoehenmodell.KEINE_DATEN)

BEV_STAND = "20250915"
BEV_URL = "https://data.bev.gv.at/download/ALS/DTM/{stand}/ALS_DTM_CRS3035RES50000m{kachel}.tif"
BEV_KACHEL = 50_000  # m
BEV_TEILFENSTER_PIXEL = 2500  # Kantenlänge je Leseauftrag in Pixeln der gelesenen Stufe
BY_METALINK = "https://geodaten.bayern.de/odd/a/dgm/dgm1/meta/metalink/09.meta4"
BY_URL = "https://download1.bayernwolke.de/a/dgm/dgm1/{name}"
BY_ERSATZ_URL = "https://download2.bayernwolke.de/a/dgm/dgm1/{name}"

GDAL_OPTIONEN = dict(
    GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
    GDAL_HTTP_MAX_RETRY=8,
    GDAL_HTTP_RETRY_DELAY=3,
    GDAL_HTTP_MULTIRANGE="YES",
    GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
    GDAL_CACHEMAX=256,  # MB
)


# ---------------------------------------------------------------- Zielraster

@dataclass(frozen=True)
class Gitter:
    """Geographisches Raster der App: Pixelmitten auf Vielfachen von schritt/teilung Bogensekunden."""

    nord: int  # Rastereinheiten (1/teilung Bogensekunde)
    west: int
    schritt: int
    teilung: int
    zeilen: int
    spalten: int

    @property
    def grad(self) -> float:
        return self.schritt / self.teilung / 3600

    @property
    def nord_grad(self) -> float:
        return self.nord / self.teilung / 3600

    @property
    def west_grad(self) -> float:
        return self.west / self.teilung / 3600

    def transform(self, z0: int = 0, s0: int = 0) -> Affine:
        """Affine Abbildung für den Ausschnitt ab Zeile z0, Spalte s0 (Zellkanten, nicht Mitten)."""
        g = self.grad
        return Affine(g, 0, self.west_grad + (s0 - 0.5) * g, 0, -g, self.nord_grad - (z0 - 0.5) * g)

    def grenzen(self) -> tuple[float, float, float, float]:
        """west, süd, ost, nord in Grad, Zellkanten eingeschlossen."""
        g = self.grad
        return (self.west_grad - g / 2, self.nord_grad - (self.zeilen - 0.5) * g,
                self.west_grad + (self.spalten - 0.5) * g, self.nord_grad + g / 2)

    def fenster(self, west: float, sued: float, ost: float, nord: float, rand: int = 2):
        """Zeilen und Spalten (z0, z1, s0, s1), die das Gradrechteck berühren; None ohne Überlappung."""
        g = self.grad
        z0 = max(0, math.floor((self.nord_grad - nord) / g + 0.5) - rand)
        z1 = min(self.zeilen, math.ceil((self.nord_grad - sued) / g + 0.5) + rand)
        s0 = max(0, math.floor((west - self.west_grad) / g + 0.5) - rand)
        s1 = min(self.spalten, math.ceil((ost - self.west_grad) / g + 0.5) + rand)
        return (z0, z1, s0, s1) if z0 < z1 and s0 < s1 else None


# ---------------------------------------------------------------- Aufsummieren

class Summen:
    """Summe und Anzahl (bzw. Maximum und Anzahl) je Zielzelle als Dateien, fortsetzbar."""

    def __init__(self, ordner: Path, name: str, gitter: Gitter, maximum: bool = False):
        self.gitter = gitter
        self.maximum = maximum
        ordner.mkdir(parents=True, exist_ok=True)
        form = (gitter.zeilen, gitter.spalten)
        self.erledigt_datei = ordner / f"{name}_erledigt.txt"
        dateien = [ordner / f"{name}_{teil}.npy" for teil in ("wert", "anzahl")]
        if self.erledigt_datei.exists() and all(d.exists() for d in dateien):
            self.wert, self.anzahl = (open_memmap(d, mode="r+") for d in dateien)
            if self.wert.shape != form:
                raise ValueError(f"{dateien[0]} passt nicht zum Raster – Arbeitsordner leeren")
            self.erledigt = set(self.erledigt_datei.read_text(encoding="utf-8").split())
        else:
            self.wert = open_memmap(dateien[0], mode="w+", dtype=np.float32, shape=form)
            self.anzahl = open_memmap(dateien[1], mode="w+", dtype=np.float32, shape=form)
            self.wert[:] = -np.inf if maximum else 0
            self.erledigt_datei.write_text("", encoding="utf-8")
            self.erledigt = set()
        self._neu: list[str] = []

    def addiere(self, hoehen: np.ndarray, transform: Affine, crs: CRS) -> None:
        """Quellraster (Lücken = QUELL_LUECKE) aufaddieren: jedes Pixel zählt in der Zielzelle,
        in der seine Mitte liegt (Quellpixel 1–8 m, Zielzellen 7–93 m)."""
        gueltig = (hoehen != QUELL_LUECKE) & np.isfinite(hoehen)
        if not gueltig.any():
            return
        zeile, spalte = self._zielindex(hoehen.shape, transform, crs)
        z = np.rint(zeile[gueltig]).astype(np.int64)
        s = np.rint(spalte[gueltig]).astype(np.int64)
        w = hoehen[gueltig].astype(np.float32)
        drin = (z >= 0) & (z < self.gitter.zeilen) & (s >= 0) & (s < self.gitter.spalten)
        if not drin.any():
            return
        z, s, w = z[drin], s[drin], w[drin]
        z0, z1, s0, s1 = z.min(), z.max() + 1, s.min(), s.max() + 1
        index = (z - z0) * (s1 - s0) + (s - s0)
        groesse = (z1 - z0) * (s1 - s0)
        form = (z1 - z0, s1 - s0)
        self.anzahl[z0:z1, s0:s1] += np.bincount(index, minlength=groesse).reshape(form).astype(np.float32)
        if self.maximum:
            m = np.full(groesse, -np.inf, np.float32)
            np.maximum.at(m, index, w)
            np.maximum(self.wert[z0:z1, s0:s1], m.reshape(form), out=self.wert[z0:z1, s0:s1])
        else:
            self.wert[z0:z1, s0:s1] += np.bincount(index, weights=w, minlength=groesse).reshape(form).astype(np.float32)

    def _zielindex(self, form: tuple[int, int], transform: Affine, crs: CRS) -> tuple[np.ndarray, np.ndarray]:
        """Gebrochene Zeile/Spalte im Zielraster für jede Pixelmitte.

        Über einige Kilometer ist die Umrechnung Projektion → Grad fast linear; ein Polynom
        2. Grades, angepasst an 5 × 5 exakt umgerechnete Punkte, trifft auf Millimeter.
        """
        hoehe, breite = form
        u = np.linspace(0, 1, 5)
        uu, vv = np.meshgrid(u, u)
        x, y = transform * (uu.ravel() * breite, vv.ravel() * hoehe)
        lon, lat = rasterio.warp.transform(crs, ETRS89, x, y)
        g = self.gitter
        zf = (g.nord_grad - np.asarray(lat)) / g.grad
        sf = (np.asarray(lon) - g.west_grad) / g.grad
        basis = lambda a, b: np.stack([np.ones_like(a), a, b, a * a, a * b, b * b], axis=-1)
        a = basis(uu.ravel(), vv.ravel())
        kz, rz, *_ = np.linalg.lstsq(a, zf, rcond=None)
        ks, rs, *_ = np.linalg.lstsq(a, sf, rcond=None)
        if np.max(np.abs(a @ kz - zf)) > 0.01 or np.max(np.abs(a @ ks - sf)) > 0.01:
            raise ValueError("Umrechnung über das Teilfenster nicht glatt genug – Teilfenster verkleinern")
        pu = ((np.arange(breite) + 0.5) / breite)[None, :]
        pv = ((np.arange(hoehe) + 0.5) / hoehe)[:, None]
        polynom = lambda k: k[0] + k[1] * pu + k[2] * pv + k[3] * pu * pu + k[4] * pu * pv + k[5] * pv * pv
        return polynom(kz), polynom(ks)

    def fertig(self, schluessel: str) -> None:
        self._neu.append(schluessel)

    def sichern(self) -> None:
        """Erst die Zahlen auf die Platte, dann die Kacheln als erledigt eintragen."""
        self.wert.flush()
        self.anzahl.flush()
        if self._neu:
            with open(self.erledigt_datei, "a", encoding="utf-8") as f:
                f.write("\n".join(self._neu) + "\n")
            self.erledigt.update(self._neu)
            self._neu = []


# ---------------------------------------------------------------- Österreich (BEV)

def bev_url(kachel: str) -> str:
    return BEV_URL.format(stand=BEV_STAND, kachel=kachel)


def bev_kacheln(gitter: Gitter) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Vorhandene BEV-Kacheln (Name, Grenzen in EPSG:3035) im Gebiet des Gitters."""
    x0, y0, x1, y1 = transform_bounds(ETRS89, LAEA, *gitter.grenzen(), densify_pts=51)
    kacheln = []
    for y in range(int(y0 // BEV_KACHEL) * BEV_KACHEL, int(y1) + 1, BEV_KACHEL):
        for x in range(int(x0 // BEV_KACHEL) * BEV_KACHEL, int(x1) + 1, BEV_KACHEL):
            name = f"N{y}E{x}"
            anfrage = urllib.request.Request(bev_url(name), method="HEAD")
            try:
                urllib.request.urlopen(anfrage, timeout=60).close()
            except urllib.error.HTTPError as fehler:
                if fehler.code == 404:
                    continue  # Kachel ganz außerhalb Österreichs
                raise
            kacheln.append((name, (max(x, x0), max(y, y0), min(x + BEV_KACHEL, x1), min(y + BEV_KACHEL, y1))))
    return kacheln


def lies_bev_teil(aufgabe: tuple[str, tuple[float, float, float, float], float]) -> tuple[np.ndarray, Affine]:
    """Ein Teilfenster einer BEV-Kachel lesen (eigene Verbindung je Aufruf, also thread-sicher)."""
    name, (x0, y0, x1, y1), aufloesung = aufgabe
    with rasterio.Env(**GDAL_OPTIONEN), rasterio.open(f"/vsicurl/{bev_url(name)}") as d:
        f = from_bounds(x0, y0, x1, y1, d.transform)
        f = f.round_offsets().round_lengths().intersection(Window(0, 0, d.width, d.height))
        form = (max(1, round(f.height / aufloesung)), max(1, round(f.width / aufloesung)))
        # Mit „average“ nimmt GDAL beim Verkleinern die passende Übersichtsstufe des COG
        # (mit „nearest“ läse es die volle Auflösung: 20-mal langsamer).
        a = d.read(1, window=f, out_shape=form, resampling=Resampling.average)
        t = d.window_transform(f) * Affine.scale(f.width / form[1], f.height / form[0])
    a[~np.isfinite(a) | (a < -1000)] = QUELL_LUECKE
    return a, t


def oesterreich(summen: Summen, aufloesung: float, parallel: int = 4) -> None:
    """BEV ALS DTM blockweise per HTTP lesen (Übersichtsstufe ≈ aufloesung m) und aufaddieren.

    Der BEV-Server liefert je Verbindung nur ca. 2–3 MB/s; deshalb mehrere Verbindungen parallel.
    """
    kacheln = bev_kacheln(summen.gitter)
    schritt = BEV_TEILFENSTER_PIXEL * aufloesung  # m: 5 km bei 2 m, 20 km bei 8 m
    aufgaben = [(f"{name}:{x:.0f}:{y:.0f}", (name, (x, y, min(x + schritt, x1), min(y + schritt, y1)), aufloesung))
                for name, (x0, y0, x1, y1) in kacheln
                for y in np.arange(y0, y1, schritt) for x in np.arange(x0, x1, schritt)]
    offen = [(s, a) for s, a in aufgaben if s not in summen.erledigt]
    print(f"  BEV: {len(kacheln)} Kacheln, {len(aufgaben)} Teilfenster, offen {len(offen)}, "
          f"Stand {BEV_STAND}, gelesen mit {aufloesung:g} m")
    zeit = time.time()
    with ThreadPoolExecutor(parallel) as pool:
        for start in range(0, len(offen), 4 * parallel):
            teil = offen[start:start + 4 * parallel]
            for (schluessel, _), (a, t) in zip(teil, pool.map(lies_bev_teil, [a for _, a in teil])):
                summen.addiere(a, t, LAEA)
                summen.fertig(schluessel)
            summen.sichern()
            fertig = start + len(teil)
            dauer = time.time() - zeit
            print(f"    {fertig}/{len(offen)} Teilfenster, {dauer / 60:.0f} min, "
                  f"noch ca. {dauer / fertig * (len(offen) - fertig) / 60:.0f} min")


# ---------------------------------------------------------------- Bayern (DGM1)

def bayern_kacheln(arbeitsordner: Path, gitter: Gitter) -> list[tuple[str, int]]:
    """Kacheln (Name, Bytes) aus der bayernweiten Metalink-Liste, die das Gitter berühren."""
    liste = arbeitsordner / "dgm1_bayern.meta4"
    if not liste.exists() or time.time() - liste.stat().st_mtime > 30 * 86400:
        print("  lade Kachelliste Bayern (ca. 22 MB)")
        urllib.request.urlretrieve(BY_METALINK, liste)
    alle = re.findall(r'<file name="((\d+)_(\d+)\.tif)">\s*<size>(\d+)</size>', liste.read_text(encoding="utf-8"))
    x0, y0, x1, y1 = transform_bounds(ETRS89, UTM32, *gitter.grenzen(), densify_pts=51)
    gesucht = []
    for name, e, n, groesse in alle:
        e, n = int(e) * 1000, int(n) * 1000
        if e + 1000 < x0 or e > x1 or n + 1000 < y0 or n > y1:
            continue
        w, s, o, no = transform_bounds(UTM32, ETRS89, e, n, e + 1000, n + 1000)
        gw, gs, go, gn = gitter.grenzen()
        if o >= gw and w <= go and no >= gs and s <= gn:
            gesucht.append((name, int(groesse)))
    return gesucht


def lade_bytes(name: str, groesse: int) -> bytes | None:
    """Eine Bayern-Kachel; bei Fehlern abwechselnd beide Spiegel, mit wachsender Pause (ca. 10 min insgesamt).
    `None`, wenn sie sich nicht laden lässt (z. B. Drosselung bei vielen gleichzeitigen Jobs)."""
    for versuch in range(12):
        url = (BY_URL if versuch % 2 == 0 else BY_ERSATZ_URL).format(name=name)
        try:
            with urllib.request.urlopen(url, timeout=120) as antwort:
                daten = antwort.read()
            if len(daten) == groesse:
                return daten
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(min(90, 5 * (versuch + 1)))
    return None


def bayern(summen: Summen, arbeitsordner: Path, parallel: int = 4) -> None:
    """DGM1-Kacheln parallel laden und nacheinander aufaddieren; nichts wird gespeichert."""
    kacheln = bayern_kacheln(arbeitsordner, summen.gitter)
    offen = [(n, g) for n, g in kacheln if n not in summen.erledigt]
    gesamt = sum(g for _, g in offen)
    print(f"  Bayern: {len(kacheln)} Kacheln im Gebiet, offen {len(offen)} ({gesamt / 1e9:.1f} GB)")
    zeit, geladen = time.time(), 0
    fehlend: list[str] = []
    with ThreadPoolExecutor(parallel) as pool:
        # höchstens 4 · parallel Kacheln gleichzeitig im Speicher
        for start in range(0, len(offen), 4 * parallel):
            teil = offen[start:start + 4 * parallel]
            for (name, groesse), daten in zip(teil, pool.map(lambda k: lade_bytes(*k), teil)):
                if daten is None:
                    fehlend.append(name)  # Copernicus füllt die Lücke; beim nächsten Lauf erneut versucht
                    continue
                with MemoryFile(daten) as speicher, speicher.open() as d:
                    a = d.read(1)
                    a[~np.isfinite(a)] = QUELL_LUECKE
                    summen.addiere(a, d.transform, d.crs or UTM32)
                summen.fertig(name)
                geladen += groesse
            summen.sichern()
            fertig = start + len(teil)
            if fertig % 240 < 4 * parallel or fertig == len(offen):
                rate = geladen / max(1e-9, time.time() - zeit)
                rest = (gesamt - geladen) / max(rate, 1)
                print(f"    {fertig}/{len(offen)} Kacheln, {geladen / 1e9:.1f} GB, "
                      f"{rate / 1e6:.1f} MB/s, noch ca. {rest / 60:.0f} min")
    if fehlend:
        print(f"  Bayern: {len(fehlend)} Kacheln nicht ladbar, dort Copernicus: {' '.join(fehlend[:20])}")


# ---------------------------------------------------------------- Schweiz und Liechtenstein (swissALTI3D)

CH_STAC = "https://data.geo.admin.ch/api/stac/v0.9/collections/ch.swisstopo.swissalti3d/items"
LV95 = CRS.from_epsg(2056)


def schweiz_kacheln(gitter: Gitter) -> list[str]:
    """Adressen der 2-m-Kacheln (1 km, COG) im Gebiet, je Kachel der neueste Stand (STAC, ohne Anmeldung)."""
    w, s, o, n = gitter.grenzen()
    url = f"{CH_STAC}?bbox={w},{s},{o},{n}&limit=100"
    neueste: dict[str, tuple[str, str]] = {}
    while url:
        with urllib.request.urlopen(url, timeout=120) as antwort:
            seite = json.load(antwort)
        for f in seite["features"]:
            m = re.match(r"swissalti3d_(\d{4})_(\d+-\d+)", f["id"])
            href = next((a["href"] for a in f["assets"].values() if a["href"].endswith(".tif") and "_2_2056_" in a["href"]), None)
            if m and href and (m[2] not in neueste or neueste[m[2]][0] < m[1]):
                neueste[m[2]] = (m[1], href)
        url = next((l["href"] for l in seite.get("links", []) if l.get("rel") == "next"), None)
    return sorted(h for _, h in neueste.values())


def lade_url(url: str) -> bytes:
    for versuch in range(6):
        try:
            with urllib.request.urlopen(url, timeout=120) as antwort:
                return antwort.read()
        except (OSError, urllib.error.URLError):
            time.sleep(2 * (versuch + 1))
    raise RuntimeError(f"{url} ließ sich nicht laden")


def schweiz(summen: Summen, parallel: int = 8) -> None:
    """swissALTI3D 2 m (Schweiz und Liechtenstein, Open Data swisstopo) laden und aufaddieren."""
    urls = schweiz_kacheln(summen.gitter)
    offen = [u for u in urls if u not in summen.erledigt]
    print(f"  Schweiz: {len(urls)} Kacheln im Gebiet, offen {len(offen)}")
    zeit = time.time()
    with ThreadPoolExecutor(parallel) as pool:
        for start in range(0, len(offen), 8 * parallel):
            teil = offen[start:start + 8 * parallel]
            for url, daten in zip(teil, pool.map(lade_url, teil)):
                with MemoryFile(daten) as speicher, speicher.open() as d:
                    a = d.read(1)
                    a[~np.isfinite(a) | (a < -1000)] = QUELL_LUECKE
                    summen.addiere(a, d.transform, d.crs or LV95)
                summen.fertig(url)
            summen.sichern()
            fertig = start + len(teil)
            if fertig % 400 < 8 * parallel or fertig == len(offen):
                print(f"    {fertig}/{len(offen)} Kacheln, {(time.time() - zeit) / 60:.0f} min")


# ---------------------------------------------------------------- Baden-Württemberg (LGL DGM1)

BW_URL = "https://opengeodata.lgl-bw.de/data/dgm/dgm1_32_{e}_{n}_2_bw.zip"
BW_GEBIET = (7.4, 47.5, 10.55, 49.85)  # West, Süd, Ost, Nord in Grad (Landesgrenze plus Rand)


def bw_kacheln(gitter: Gitter) -> list[tuple[int, int]]:
    """2-km-Kacheln (Ostwert ungerade, Nordwert gerade in km, EPSG:25832), die Gitter und Land berühren."""
    gw, gs, go, gn = gitter.grenzen()
    w, s, o, n = max(gw, BW_GEBIET[0]), max(gs, BW_GEBIET[1]), min(go, BW_GEBIET[2]), min(gn, BW_GEBIET[3])
    if w >= o or s >= n:
        return []
    x0, y0, x1, y1 = transform_bounds(ETRS89, UTM32, w, s, o, n, densify_pts=51)
    e0 = int(x0 // 1000) - 1
    e0 -= (e0 + 1) % 2  # ungerade
    n0 = int(y0 // 1000) - 2
    n0 -= n0 % 2  # gerade
    return [(e, nn) for nn in range(n0, int(y1 // 1000) + 1, 2) for e in range(e0, int(x1 // 1000) + 1, 2)]


def lies_bw(aufgabe: tuple[int, int, int]) -> list[tuple[np.ndarray, Affine]] | None:
    """Eine 2-km-Kachel (ZIP mit vier 1-km-XYZ-Dateien, 1 m), je km auf [faktor] m gemittelt; `None`, wenn
    es sie nicht gibt (außerhalb des Landes)."""
    import io
    import zipfile
    e, n, faktor = aufgabe
    url = BW_URL.format(e=e, n=n)
    daten = None
    for versuch in range(8):
        try:
            with urllib.request.urlopen(url, timeout=180) as antwort:
                daten = antwort.read()
            break
        except urllib.error.HTTPError as fehler:
            if fehler.code == 404:
                return None
            time.sleep(min(60, 5 * (versuch + 1)))
        except (OSError, urllib.error.URLError):
            time.sleep(min(60, 5 * (versuch + 1)))
    if daten is None:
        raise RuntimeError(f"{url} ließ sich nicht laden")
    teile = []
    with zipfile.ZipFile(io.BytesIO(daten)) as z:
        for name in z.namelist():
            if not name.endswith(".xyz"):
                continue
            # Textmodus von fromstring parst in C (np.array(text.split()) wäre ca. 10-mal langsamer)
            werte = np.fromstring(z.read(name).decode("ascii"), dtype=np.float64, sep=" ").reshape(-1, 3)
            x0 = math.floor(werte[:, 0].min())
            y1 = math.ceil(werte[:, 1].max())
            spalten = math.ceil(werte[:, 0].max()) - x0
            zeilen = y1 - math.floor(werte[:, 1].min())
            raster = np.full((zeilen, spalten), np.nan, np.float32)
            raster[(y1 - werte[:, 1]).astype(np.int64), (werte[:, 0] - x0).astype(np.int64)] = werte[:, 2]
            # Auf faktor × faktor m mitteln (weniger Punkte zum Aufaddieren, Zielzellen 7–30 m)
            zh, sh = zeilen // faktor, spalten // faktor
            bloecke = raster[:zh * faktor, :sh * faktor].reshape(zh, faktor, sh, faktor)
            with np.errstate(invalid="ignore"):
                mittel = np.nanmean(bloecke, axis=(1, 3)) if np.isnan(bloecke).any() else bloecke.mean(axis=(1, 3))
            mittel[~np.isfinite(mittel)] = QUELL_LUECKE
            teile.append((mittel.astype(np.float32), Affine(faktor, 0, x0, 0, -faktor, y1)))
    return teile


def baden_wuerttemberg(summen: Summen, aufloesung: int = 5, parallel: int = 12) -> None:
    """LGL DGM1 (Baden-Württemberg, dl-de/by-2-0): 2-km-Kacheln als ZIP mit XYZ-Text (ca. 13 MB).

    Der Server liefert je Verbindung nur ca. 1 MB/s; deshalb viele Verbindungen parallel. Kacheln außerhalb des
    Landes gibt es nicht (404) und werden übersprungen.
    """
    kacheln = bw_kacheln(summen.gitter)
    offen = [k for k in kacheln if f"bw:{k[0]}:{k[1]}" not in summen.erledigt]
    print(f"  Baden-Württemberg: {len(kacheln)} mögliche Kacheln, offen {len(offen)}, gemittelt auf {aufloesung} m")
    zeit, vorhanden = time.time(), 0
    with ThreadPoolExecutor(parallel) as pool:
        for start in range(0, len(offen), 4 * parallel):
            teil = offen[start:start + 4 * parallel]
            for (e, n), ergebnis in zip(teil, pool.map(lies_bw, [(e, n, aufloesung) for e, n in teil])):
                for a, t in ergebnis or []:
                    summen.addiere(a, t, UTM32)
                vorhanden += ergebnis is not None
                summen.fertig(f"bw:{e}:{n}")
            summen.sichern()
            fertig = start + len(teil)
            if fertig % 240 < 4 * parallel or fertig == len(offen):
                dauer = time.time() - zeit
                print(f"    {fertig}/{len(offen)} Kacheln ({vorhanden} im Land), {dauer / 60:.0f} min, "
                      f"noch ca. {dauer / fertig * (len(offen) - fertig) / 60:.0f} min")


# ---------------------------------------------------------------- Elsass (IGN RGE ALTI)

IGN_WMS = ("https://data.geopf.fr/wms-r?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap"
           "&LAYERS=ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES&STYLES=&CRS=EPSG:4326"
           "&BBOX={sued},{west},{nord},{ost}&WIDTH={breite}&HEIGHT={hoehe}&FORMAT=image/geotiff")
IGN_BLOCK = 4096  # Pixel je Kante und Abruf (der Dienst liefert bis 4096², ca. 9 MB/s)
ELSASS_POLY = "https://download.geofabrik.de/europe/france/alsace.poly"
WGS84 = CRS.from_epsg(4326)


def poly_ringe(text: str) -> list[list[tuple[float, float]]]:
    """Äußere Ringe einer Osmosis-.poly-Datei (Länge, Breite); Löcher (Name mit „!“) entfallen."""
    ringe, ring, aussen = [], None, True
    for zeile in text.splitlines()[1:]:
        zeile = zeile.strip()
        if not zeile:
            continue
        if ring is None:
            if zeile == "END":
                break
            ring, aussen = [], not zeile.startswith("!")
        elif zeile == "END":
            if aussen and len(ring) >= 3:
                ringe.append(ring)
            ring = None
        else:
            l, b = zeile.split()[:2]
            ring.append((float(l), float(b)))
    return ringe


def ausschnitt_maske(ringe: list[list[tuple[float, float]]], form: tuple[int, int], transform: Affine) -> np.ndarray:
    """True für Pixel, deren Mitte in einem der Ringe liegt (Grad, gleiche Abbildung wie das Raster)."""
    from rasterio.features import rasterize
    formen = [({"type": "Polygon", "coordinates": [r]}, 1) for r in ringe]
    return rasterize(formen, out_shape=form, transform=transform, fill=0, dtype="uint8").astype(bool)


def lies_ign(aufgabe: tuple[float, float, float, float, int, int]) -> tuple[np.ndarray, Affine]:
    west, sued, ost, nord, breite, hoehe = aufgabe
    url = IGN_WMS.format(west=west, sued=sued, ost=ost, nord=nord, breite=breite, hoehe=hoehe)
    daten = lade_url(url)
    with MemoryFile(daten) as speicher, speicher.open() as d:
        a = d.read(1).astype(np.float32)
        luecke = d.nodata
    if a.shape != (hoehe, breite):
        raise RuntimeError(f"IGN: {a.shape} statt {(hoehe, breite)}")
    a[~np.isfinite(a) | (a < -1000) | ((a == luecke) if luecke is not None else False)] = QUELL_LUECKE
    # Abbildung aus der angefragten Box (die Datei nennt eine „unknown“-Abbildung mit Breite zuerst)
    return a, rasterio.transform.from_bounds(west, sued, ost, nord, breite, hoehe)


def elsass(summen: Summen, proben: int = 2, parallel: int = 4) -> None:
    """IGN RGE ALTI (1–5 m, Etalab 2.0) über den WMS der Géoplateforme, nur im Elsass.

    Der Dienst liefert auch jenseits der Grenze Werte (andere Quellen); deshalb zählt nur, was im Umriss
    des Elsass (Geofabrik, ca. 1 km Rand) liegt. Je Zielzelle [proben]² Abtastpunkte.
    """
    ringe = poly_ringe(lade_url(ELSASS_POLY).decode("utf-8"))
    ew = min(l for r in ringe for l, _ in r)
    eo = max(l for r in ringe for l, _ in r)
    es = min(b for r in ringe for _, b in r)
    en = max(b for r in ringe for _, b in r)
    gw, gs, go, gn = summen.gitter.grenzen()
    w, s, o, n = max(gw, ew), max(gs, es), min(go, eo), min(gn, en)
    if w >= o or s >= n:
        print("  Elsass: außerhalb der Kachel")
        return
    schritt = summen.gitter.grad / proben
    kante = IGN_BLOCK * schritt
    aufgaben = []
    for b0 in np.arange(s, n, kante):
        for l0 in np.arange(w, o, kante):
            b1, l1 = min(b0 + kante, n), min(l0 + kante, o)
            box = (float(l0), float(b0), float(l1), float(b1), max(1, round((l1 - l0) / schritt)), max(1, round((b1 - b0) / schritt)))
            # Blöcke ganz außerhalb des Umrisses auslassen (Prüfung auf grobem Raster)
            grob = ausschnitt_maske(ringe, (32, 32), rasterio.transform.from_bounds(l0, b0, l1, b1, 32, 32))
            if grob.any():
                aufgaben.append((f"ign:{l0:.4f}:{b0:.4f}", box))
    offen = [(k, a) for k, a in aufgaben if k not in summen.erledigt]
    print(f"  Elsass (IGN): {len(aufgaben)} Blöcke, offen {len(offen)}")
    zeit = time.time()
    with ThreadPoolExecutor(parallel) as pool:
        for start in range(0, len(offen), parallel):
            teil = offen[start:start + parallel]
            for (schluessel, box), (a, t) in zip(teil, pool.map(lies_ign, [b for _, b in teil])):
                a[~ausschnitt_maske(ringe, a.shape, t)] = QUELL_LUECKE
                summen.addiere(a, t, WGS84)
                summen.fertig(schluessel)
            summen.sichern()
            print(f"    {start + len(teil)}/{len(offen)} Blöcke, {(time.time() - zeit) / 60:.0f} min")


# ---------------------------------------------------------------- Niederlande (AHN, PDOK)

RD = CRS.from_epsg(28992)
AHN_WCS = ("https://service.pdok.nl/rws/ahn/wcs/v1_0?SERVICE=WCS&VERSION=2.0.1&REQUEST=GetCoverage&COVERAGEID=dtm_05m"
           "&FORMAT=image/tiff&SUBSET=x({x0},{x1})&SUBSET=y({y0},{y1})&SCALESIZE=x({n}),y({n})")
AHN_BLOCK = 5000  # m je Abruf
AHN_AUFLOESUNG = 5  # m (der Dienst mittelt das 0,5-m-Modell herunter)


def lies_ahn(aufgabe: tuple[int, int]) -> tuple[np.ndarray, Affine]:
    x0, y0 = aufgabe
    n = AHN_BLOCK // AHN_AUFLOESUNG
    url = AHN_WCS.format(x0=x0, x1=x0 + AHN_BLOCK, y0=y0, y1=y0 + AHN_BLOCK, n=n)
    daten = None
    for versuch in range(6):
        try:
            with urllib.request.urlopen(url, timeout=120) as antwort:
                daten = antwort.read()
            break
        except urllib.error.HTTPError as fehler:
            if 400 <= fehler.code < 500:
                # Block außerhalb der Ausdehnung des Modells: der Dienst antwortet mit einem Fehler statt Lücken
                return np.full((1, 1), QUELL_LUECKE, np.float32), Affine(AHN_BLOCK, 0, x0, 0, -AHN_BLOCK, y0 + AHN_BLOCK)
            time.sleep(3 * (versuch + 1))
        except (OSError, urllib.error.URLError):
            time.sleep(3 * (versuch + 1))
    if daten is None:
        raise RuntimeError(f"{url} ließ sich nicht laden")
    with MemoryFile(daten) as speicher, speicher.open() as d:
        a = d.read(1).astype(np.float32)
        t = d.transform
    # Lücken (Gebäude, Wasser, außerhalb der Niederlande): größter float32-Wert
    a[~np.isfinite(a) | (a > 1e30) | (a < -1000)] = QUELL_LUECKE
    return a, t


def niederlande(summen: Summen, parallel: int = 6) -> None:
    """AHN4 DTM (Actueel Hoogtebestand Nederland, CC0) über den WCS von PDOK, 5-km-Blöcke mit 5 m."""
    x0, y0, x1, y1 = transform_bounds(ETRS89, RD, *summen.gitter.grenzen(), densify_pts=51)
    # Ausdehnung von AHN in RD (ca. 10–280 km Ost, 305–620 km Nord)
    x0, y0, x1, y1 = max(x0, 10_000), max(y0, 305_000), min(x1, 280_000), min(y1, 620_000)
    if x0 >= x1 or y0 >= y1:
        print("  Niederlande: außerhalb der Kachel")
        return
    aufgaben = [(int(x), int(y)) for y in range(int(y0 // AHN_BLOCK) * AHN_BLOCK, int(y1), AHN_BLOCK)
                for x in range(int(x0 // AHN_BLOCK) * AHN_BLOCK, int(x1), AHN_BLOCK)]
    offen = [a for a in aufgaben if f"ahn:{a[0]}:{a[1]}" not in summen.erledigt]
    print(f"  Niederlande (AHN): {len(aufgaben)} Blöcke, offen {len(offen)}")
    zeit = time.time()
    with ThreadPoolExecutor(parallel) as pool:
        for start in range(0, len(offen), 4 * parallel):
            teil = offen[start:start + 4 * parallel]
            for (x, y), (a, t) in zip(teil, pool.map(lies_ahn, teil)):
                summen.addiere(a, t, RD)
                summen.fertig(f"ahn:{x}:{y}")
            summen.sichern()
            print(f"    {start + len(teil)}/{len(offen)} Blöcke, {(time.time() - zeit) / 60:.0f} min")


# ---------------------------------------------------------------- Niedersachsen (LGLN DGM1)

NI_STAC = "https://dgm.stac.lgln.niedersachsen.de/search"


def niedersachsen_kacheln(gitter: Gitter) -> list[str]:
    """Adressen der 1-km-Kacheln (COG) im Gebiet aus dem STAC-Katalog des LGLN."""
    w, s, o, n = gitter.grenzen()
    url = f"{NI_STAC}?collections=dgm1&bbox={w},{s},{o},{n}&limit=500"
    adressen: set[str] = set()
    while url:
        seite = json.loads(lade_url(url))
        for f in seite["features"]:
            href = next((a["href"] for a in f["assets"].values() if a["href"].endswith(".tif")), None)
            if href:
                adressen.add(href)
        url = next((l["href"] for l in seite.get("links", []) if l.get("rel") == "next"), None)
    return sorted(adressen)


def lies_cog(aufgabe: tuple[str, float]) -> tuple[np.ndarray, Affine, CRS]:
    """Ganze COG-Kachel in der Auflösung [aufloesung] m (GDAL nimmt die passende Übersichtsstufe)."""
    url, aufloesung = aufgabe
    for versuch in range(6):
        try:
            with rasterio.Env(**GDAL_OPTIONEN), rasterio.open(f"/vsicurl/{url}") as d:
                form = (max(1, round(d.height * d.res[1] / aufloesung)), max(1, round(d.width * d.res[0] / aufloesung)))
                a = d.read(1, out_shape=form, resampling=Resampling.average, masked=True)
                t = d.transform * Affine.scale(d.width / form[1], d.height / form[0])
                return np.ma.filled(a.astype(np.float32), QUELL_LUECKE), t, d.crs or UTM32
        except rasterio.errors.RasterioIOError:
            time.sleep(3 * (versuch + 1))
    raise RuntimeError(f"{url} ließ sich nicht lesen")


def niedersachsen(summen: Summen, aufloesung: float = 5, parallel: int = 8) -> None:
    """DGM1 Niedersachsen (LGLN, dl-de/by-2-0) aus dem STAC-Katalog, gelesen mit [aufloesung] m."""
    urls = niedersachsen_kacheln(summen.gitter)
    offen = [u for u in urls if u not in summen.erledigt]
    print(f"  Niedersachsen: {len(urls)} Kacheln im Gebiet, offen {len(offen)}")
    zeit = time.time()
    with ThreadPoolExecutor(parallel) as pool:
        for start in range(0, len(offen), 8 * parallel):
            teil = offen[start:start + 8 * parallel]
            for url, (a, t, crs) in zip(teil, pool.map(lies_cog, [(u, aufloesung) for u in teil])):
                a[(a < -1000) | ~np.isfinite(a)] = QUELL_LUECKE
                summen.addiere(a, t, crs)
                summen.fertig(url)
            summen.sichern()
            fertig = start + len(teil)
            if fertig % 800 < 8 * parallel or fertig == len(offen):
                print(f"    {fertig}/{len(offen)} Kacheln, {(time.time() - zeit) / 60:.0f} min")


# ---------------------------------------------------------------- Ergebnis

def mittelwert_in_bloecken(summen: Summen, ziel: np.ndarray, faktor: int, zeilen_je_block: int = 512) -> int:
    """Mittelwert = Summe / Anzahl, gerundet auf 1/faktor m, in ziel (int16) schreiben; Lücken bleiben."""
    luecken = 0
    for z0 in range(0, ziel.shape[0], zeilen_je_block):
        z1 = min(ziel.shape[0], z0 + zeilen_je_block)
        n = np.asarray(summen.anzahl[z0:z1])
        s = np.asarray(summen.wert[z0:z1])
        block = np.full(n.shape, KEINE_DATEN, np.int16)
        gut = n > 0.5  # mindestens ein halbes Quellpixel
        block[gut] = np.rint(s[gut] / n[gut] * faktor).astype(np.int16)
        ziel[z0:z1] = block
        luecken += int((~gut).sum())
    return luecken


def freier_platz(pfad: Path) -> float:
    pfad.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(pfad).free / 1e9
