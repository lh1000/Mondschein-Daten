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
