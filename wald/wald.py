"""Walddatei für das Licht am Weg (docs/konzept.md, Meilenstein 23; notizen/recherche/walddichte.md).

Schreibt `mondschein-wald.bin` (Format MSWALD1, siehe unten): für jede 1/3″-Zelle (ca. 10 m × 7 m),
durch die ein Weg des Wegenetzes läuft, und ihre 8 Nachbarn

- die Lückenquote P₀ (Anteil der Pixel unter 2 m) und die mittlere Kronenhöhe aus Meta/WRI CHMv2
  (Baumhöhen 1,2 m, global, AWS S3 ohne Anmeldung),
- den Blatttyp (Nadel, Laub, gemischt) aus Copernicus HRL Dominant Leaf Type, wenn die Datei(en)
  mit --blatttyp angegeben sind (Anmeldung nötig, siehe tools/licht/README-wald.md); sonst „unbekannt“.

Quellen:
    Canopy height: Meta and World Resources Institute (2026), CHMv2, CC BY 4.0, verändert.
    Generated using European Union's Copernicus Land Monitoring Service information (DLT).

Aufruf (venv aus tools/hoehen; in der Cloud /opt/mondschein/venv):
    python wald.py --wege mondschein-wege.bin --ausgabe mondschein-wald.bin
                   [--blatttyp DLT_2018_010m_E44N26_03035_v020.tif …] [--zwischenspeicher chm/]
                   [--gebiet süd,nord,west,ost] [--pruefen licht-luecke.bin]
Ohne --zwischenspeicher liest das Werkzeug nur die nötigen Blöcke über HTTP (gut für kleine Gebiete);
mit lädt es jede CHMv2-Kachel (ca. 225 MB) ganz und löscht sie danach wieder (schneller fürs Kerngebiet).

Format MSWALD1 (Little Endian):
    Kopf, 32 Byte: Magic "MSWALD1\\0", Version (int32, 1), Teilung (int32, Zellen je Grad = 10800),
                   Kachelkante (int32, 256), Anzahl Kacheln (int32), Stand (int32, JJJJMMTT),
                   Quellen (int32, Bit 0 CHMv2, Bit 1 Blatttyp aus DLT)
    Verzeichnis:   je Kachel Kachelzeile, Kachelspalte, Offset, Länge (4 × int32), aufsteigend nach
                   Zeile, dann Spalte
    Kacheln:       zlib (Deflate) aus 2 × 65536 Byte: erst P₀, dann Typ/Höhe, je 256 × 256 Zellen
    Zelle:         Zeile z = ⌊Breite · 10800 + ½⌋, Spalte s = ⌊(Länge + 180) · 10800 + ½⌋ (Grad), also die
                   Zelle um den Rasterpunkt wie in der Höhendatei;
                   Kachel (z div 256, s div 256), in der Kachel Index (z mod 256) · 256 + s mod 256
    Byte 1:        P₀ in Prozent (0–100), 255 = keine Angabe
    Byte 2:        Bits 0–1 Blatttyp (0 unbekannt/kein Baum, 1 Nadel, 2 Laub, 3 gemischt),
                   Bits 2–7 Kronenhöhe in Metern (0–63)
Leser in core: `io.github.lh1000.mondschein.core.licht.Walddatei`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window

MAGIC = b"MSWALD1\0"
VERSION = 1
KOPF = 32
TEILUNG = 10_800  # Zellen je Grad (1/3″)
KANTE = 256
KEINE_ANGABE = 255
QUELLE_CHM = 1
QUELLE_DLT = 2
UNBEKANNT, NADEL, LAUB, GEMISCHT = range(4)

CHM = "https://dataforgood-fb-data.s3.amazonaws.com/forests/v2/global/dinov3_global_chm_v2_ml3/chm/{qk}.tif"
ZOOM = 10
MERKATOR_R = 6_378_137.0
MERKATOR_RAND = math.pi * MERKATOR_R
BLOCK = 512
BAUM = 2  # Meter: ab hier zählt ein Pixel als Krone (wie vegetation.py)
KRONE_MAX = 50  # Meter: höher ist ein Messfehler, das Pixel zählt nicht
PIXEL_MIN = 20  # gültige Pixel je Zelle (von ca. 100), sonst keine Angabe
ABTASTUNG = 2.0  # Meter zwischen zwei Punkten beim Legen der Wege aufs Raster
ERDRADIUS = 6_371_000.0
KERN = (47.25, 47.90, 10.70, 13.15)


# ------------------------------------------------------------------ Wegzellen


def wegpunkte(pfad: Path) -> tuple[np.ndarray, np.ndarray]:
    """Alle Wege der Wegedatei (MSWEGE1, tools/wege/README.md) als dicht abgetastete Punkte (Grad)."""
    roh = np.fromfile(pfad, dtype=np.uint8)
    magic = bytes(roh[:8])
    if magic != b"MSWEGE1\0":
        raise SystemExit(f"Keine Wegedatei: {pfad}")
    _, n, m, geo, *_ = struct.unpack_from("<iiiii", roh, 8)
    knoten = np.frombuffer(roh, dtype=[("b", "<i4"), ("l", "<i4"), ("z", "<i4")], count=n, offset=64)
    o = 64 + 12 * n
    kanten = np.frombuffer(roh, dtype=[("von", "<i4"), ("nach", "<i4"), ("laenge", "<f4"), ("auf", "<u2"),
                                       ("ab", "<u2"), ("art", "u1"), ("sac", "u1"), ("sicht", "u1"),
                                       ("frei", "u1"), ("start", "<i4"), ("name", "<i4")], count=m, offset=o)
    o += 28 * m
    o += -o % 4
    deltas = np.frombuffer(roh, dtype="<i2", count=2 * geo, offset=o).reshape(geo, 2).astype(np.int64)

    # Knoten in 1/100 000 Grad gerundet wie die Geometrie (Leser in core: Wegenetz.geometrie)
    kb = np.floor(knoten["b"] / 10 + 0.5).astype(np.int64)
    kl = np.floor(knoten["l"] / 10 + 0.5).astype(np.int64)
    start = kanten["start"].astype(np.int64)
    anzahl = np.diff(np.append(start, geo))
    von, nach = kanten["von"], kanten["nach"]
    # Punkte je Kante: Startknoten, Geometrie, Endknoten
    ab = start + 2 * np.arange(m)
    gesamt = geo + 2 * m
    pb = np.empty(gesamt, dtype=np.int64)
    pl = np.empty(gesamt, dtype=np.int64)
    pb[ab], pl[ab] = kb[von], kl[von]
    pb[ab + anzahl + 1], pl[ab + anzahl + 1] = kb[nach], kl[nach]
    if geo:
        kante = np.repeat(np.arange(m), anzahl)
        summe = np.cumsum(deltas, axis=0)
        vorher = np.vstack([[0, 0], summe])[start]  # Summe vor der ersten Geometrie jeder Kante
        stelle = np.arange(geo) + 2 * kante + 1
        pb[stelle] = kb[von][kante] + summe[:, 0] - vorher[kante, 0]
        pl[stelle] = kl[von][kante] + summe[:, 1] - vorher[kante, 1]
    b = pb / 1e5
    l = pl / 1e5
    # Strecken innerhalb einer Kante (nicht vom Ende einer Kante zum Anfang der nächsten)
    ende = np.zeros(gesamt, dtype=bool)
    ende[ab + anzahl + 1] = True
    i = np.flatnonzero(~ende[:-1])
    cosb = np.cos(np.radians(b[i]))
    d = np.hypot(b[i + 1] - b[i], (l[i + 1] - l[i]) * cosb) * math.pi / 180 * ERDRADIUS
    schritte = np.maximum(1, np.ceil(d / ABTASTUNG).astype(np.int64))
    j = np.repeat(i, schritte)
    t = (np.arange(schritte.sum()) - np.repeat(np.cumsum(schritte) - schritte, schritte)) / np.repeat(schritte, schritte)
    sb = b[j] + (b[j + 1] - b[j]) * t
    sl = l[j] + (l[j + 1] - l[j]) * t
    # Endpunkte jeder Kante mit dazu
    return np.concatenate([sb, b[ende]]), np.concatenate([sl, l[ende]])


def zellen(b: np.ndarray, l: np.ndarray, gebiet: tuple[float, float, float, float]) -> np.ndarray:
    """Schlüssel (Zeile · 2²⁴ + Spalte) der Zellen unter den Punkten im Gebiet und ihrer 8 Nachbarn, sortiert."""
    sued, nord, west, ost = gebiet
    innen = (b >= sued) & (b <= nord) & (l >= west) & (l <= ost)
    z, s = zelle(b[innen], l[innen])
    kern = np.unique(z << 24 | s)
    teile = [kern]
    for dz in (-1, 0, 1):
        for ds in (-1, 0, 1):
            if dz or ds:
                teile.append(kern + (dz << 24) + ds)
    return np.unique(np.concatenate(teile))


def zelle(breite: np.ndarray, laenge: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Zeile und Spalte der Zelle um den nächsten 1/3″-Rasterpunkt (Grad)."""
    return (np.floor(breite * TEILUNG + 0.5).astype(np.int64),
            np.floor((laenge + 180) * TEILUNG + 0.5).astype(np.int64))


def mitte(schluessel: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Breite und Länge (Grad) der Zellmitten."""
    z, s = zeile_spalte(schluessel)
    return z / TEILUNG, s / TEILUNG - 180


def zeile_spalte(schluessel: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    return schluessel >> 24, schluessel & 0xFFFFFF


# ------------------------------------------------------------------ CHMv2


def quadkey(x: int, y: int, z: int = ZOOM) -> str:
    return "".join(str(((x >> (i - 1)) & 1) + 2 * ((y >> (i - 1)) & 1)) for i in range(z, 0, -1))


def kachel_xy(breite: np.ndarray, laenge: np.ndarray, z: int = ZOOM) -> tuple[np.ndarray, np.ndarray]:
    n = 2 ** z
    x = np.floor((laenge + 180) / 360 * n).astype(np.int64)
    y = np.floor((1 - np.arcsinh(np.tan(np.radians(breite))) / math.pi) / 2 * n).astype(np.int64)
    return x, y


class Summen:
    """Pixelzählung je gesuchter Zelle: gültige Pixel, davon offen (< 2 m), Kronenpixel und ihre Höhensumme."""

    def __init__(self, schluessel: np.ndarray):
        self.schluessel = schluessel
        self.gueltig = np.zeros(len(schluessel), dtype=np.int32)
        self.offen = np.zeros(len(schluessel), dtype=np.int32)
        self.krone = np.zeros(len(schluessel), dtype=np.int32)
        self.hoehe = np.zeros(len(schluessel), dtype=np.int64)

    def block(self, werte: np.ndarray, zellzeile: np.ndarray, zellspalte: np.ndarray) -> None:
        """Ein Block Pixel; [zellzeile] je Pixelzeile, [zellspalte] je Pixelspalte (beide monoton)."""
        v = werte.astype(np.int32)
        gueltig = v <= KRONE_MAX
        offen = v < BAUM
        krone = gueltig & ~offen
        za = np.flatnonzero(np.diff(zellzeile, prepend=zellzeile[0] - 1))
        sa = np.flatnonzero(np.diff(zellspalte, prepend=zellspalte[0] - 1))
        schl = (zellzeile[za][:, None] << 24 | zellspalte[sa][None, :]).ravel()
        i = np.searchsorted(self.schluessel, schl)
        i[i == len(self.schluessel)] = 0
        treffer = self.schluessel[i] == schl
        if not treffer.any():
            return

        def zelle(a: np.ndarray) -> np.ndarray:
            return np.add.reduceat(np.add.reduceat(a, za, axis=0), sa, axis=1).ravel()[treffer]

        k = i[treffer]
        self.gueltig[k] += zelle(gueltig.astype(np.int32))
        self.offen[k] += zelle(offen.astype(np.int32))
        self.krone[k] += zelle(krone.astype(np.int32))
        self.hoehe[k] += zelle(np.where(krone, v, 0))


def chm_lesen(summen: Summen, zwischenspeicher: Path | None) -> int:
    """Alle CHMv2-Kacheln über den gesuchten Zellen blockweise zählen; Anzahl gelesener Blöcke."""
    z, s = zeile_spalte(summen.schluessel)
    breite, laenge = mitte(summen.schluessel)
    kx, ky = kachel_xy(breite, laenge)
    kacheln = np.unique(kx << 16 | ky)
    beginn = time.time()
    bloecke = 0
    for nr, k in enumerate(kacheln):
        x, y = int(k >> 16), int(k & 0xFFFF)
        qk = quadkey(x, y)
        url = CHM.format(qk=qk)
        datei = None
        if zwischenspeicher is not None:
            datei = zwischenspeicher / f"{qk}.tif"
            if not datei.exists():
                try:
                    laden(url, datei)
                except urllib.error.HTTPError as fehler:
                    print(f"  Kachel {qk}: {fehler} – übersprungen")
                    continue
        quelle = str(datei) if datei else f"/vsicurl/{url}"
        innen = (kx == x) & (ky == y)
        try:
            n = kachel_lesen(summen, quelle, z[innen], s[innen])
        except rasterio.errors.RasterioIOError as fehler:
            print(f"  Kachel {qk}: {fehler} – übersprungen")
            continue
        bloecke += n
        if datei is not None:
            datei.unlink()
        print(f"  Kachel {nr + 1}/{len(kacheln)} ({qk}): {n} Blöcke ({time.time() - beginn:.0f} s)", flush=True)
    return bloecke


def laden(url: str, ziel: Path) -> None:
    ziel.parent.mkdir(parents=True, exist_ok=True)
    teil = ziel.with_name(ziel.name + ".teil")
    with urllib.request.urlopen(url, timeout=120) as antwort, open(teil, "wb") as f:
        while block := antwort.read(8 << 20):
            f.write(block)
    teil.replace(ziel)


def kachel_lesen(summen: Summen, quelle: str, z: np.ndarray, s: np.ndarray) -> int:
    """Die Blöcke einer Kachel, die gesuchte Zellen berühren (alle vier Ecken jeder Zelle)."""
    with rasterio.open(quelle) as ds:
        t = ds.transform
        links, oben, aufloesung = t.c, t.f, t.a
        # Pixel der Zellecken: Länge linear in x, Breite über Mercator in y
        bloecke = set()
        for dz in (-0.5, 0.5):
            for ds_ in (-0.5, 0.5):
                lon = (s + ds_) / TEILUNG - 180
                lat = (z + dz) / TEILUNG
                px = (np.radians(lon) * MERKATOR_R - links) / aufloesung
                py = (oben - np.arcsinh(np.tan(np.radians(lat))) * MERKATOR_R) / aufloesung
                bx = np.clip(np.floor(px).astype(np.int64) // BLOCK, 0, ds.width // BLOCK - 1)
                by = np.clip(np.floor(py).astype(np.int64) // BLOCK, 0, ds.height // BLOCK - 1)
                bloecke.update((by << 16 | bx).tolist())
        for b in sorted(bloecke):
            by, bx = b >> 16, b & 0xFFFF
            werte = ds.read(1, window=Window(bx * BLOCK, by * BLOCK, BLOCK, BLOCK))
            # Pixelmitten → Zellen
            x = links + (bx * BLOCK + np.arange(werte.shape[1]) + 0.5) * aufloesung
            y = oben - (by * BLOCK + np.arange(werte.shape[0]) + 0.5) * aufloesung
            lon = np.degrees(x / MERKATOR_R)
            lat = np.degrees(np.arctan(np.sinh(y / MERKATOR_R)))
            summen.block(werte, *zelle(lat, lon))
        return len(bloecke)


# ------------------------------------------------------------------ Blatttyp (Copernicus DLT)


def blatttyp(schluessel: np.ndarray, dateien: list[Path]) -> np.ndarray:
    """Blatttyp je Zelle: Mehrheit im 3 × 3-Fenster (10-m-Pixel) um die Zellmitte; 0 ohne Angabe."""
    from rasterio.warp import transform

    breite, laenge = mitte(schluessel)
    laub = np.zeros(len(schluessel), dtype=np.int16)
    nadel = np.zeros(len(schluessel), dtype=np.int16)
    for pfad in dateien:
        with rasterio.open(pfad) as ds:
            xs, ys = transform("EPSG:4326", ds.crs, laenge, breite)
            t = ~ds.transform
            xs, ys = np.asarray(xs), np.asarray(ys)
            spalte = np.floor(t.a * xs + t.b * ys + t.c).astype(np.int64)
            zeile = np.floor(t.d * xs + t.e * ys + t.f).astype(np.int64)
            innen = (zeile >= 1) & (zeile < ds.height - 1) & (spalte >= 1) & (spalte < ds.width - 1)
            if not innen.any():
                print(f"  {pfad.name}: keine Wegzelle")
                continue
            z0, z1 = zeile[innen].min() - 1, zeile[innen].max() + 2
            s0, s1 = spalte[innen].min() - 1, spalte[innen].max() + 2
            werte = ds.read(1, window=Window(s0, z0, s1 - s0, z1 - z0))
            zi, si = zeile[innen] - z0, spalte[innen] - s0
            for dz in (-1, 0, 1):
                for dsp in (-1, 0, 1):
                    v = werte[zi + dz, si + dsp]
                    laub[innen] += v == 1
                    nadel[innen] += v == 2
            print(f"  {pfad.name}: {int(innen.sum())} Zellen")
    typ = np.full(len(schluessel), UNBEKANNT, dtype=np.uint8)
    typ[(laub > 0) | (nadel > 0)] = GEMISCHT
    typ[nadel >= 2 * laub] = NADEL
    typ[laub >= 2 * nadel] = LAUB
    typ[(laub == 0) & (nadel == 0)] = UNBEKANNT
    return typ


# ------------------------------------------------------------------ Schreiben


def werte(summen: Summen, typ: np.ndarray | None) -> tuple[np.ndarray, np.ndarray]:
    """Byte 1 (P₀ in %) und Byte 2 (Typ | Höhe << 2) je Zelle."""
    g = summen.gueltig
    p0 = np.full(len(g), KEINE_ANGABE, dtype=np.uint8)
    mit = g >= PIXEL_MIN
    p0[mit] = np.floor(summen.offen[mit] * 100 / g[mit] + 0.5).astype(np.uint8)
    hoehe = np.zeros(len(g), dtype=np.int64)
    k = summen.krone > 0
    hoehe[k] = np.floor(summen.hoehe[k] / summen.krone[k] + 0.5)
    hoehe = np.clip(hoehe, 0, 63)
    t = typ if typ is not None else np.zeros(len(g), dtype=np.uint8)
    zweites = (t.astype(np.int64) & 3) | hoehe << 2
    zweites[~mit] = 0
    return p0, zweites.astype(np.uint8)


def schreiben(pfad: Path, schluessel: np.ndarray, p0: np.ndarray, zweites: np.ndarray, stand: str,
              quellen: int) -> int:
    z, s = zeile_spalte(schluessel)
    kachel = (z // KANTE) << 24 | (s // KANTE)
    reihenfolge = np.argsort(kachel, kind="stable")
    z, s, kachel, p0, zweites = z[reihenfolge], s[reihenfolge], kachel[reihenfolge], p0[reihenfolge], zweites[reihenfolge]
    grenzen = np.flatnonzero(np.diff(kachel, prepend=-1))
    grenzen = np.append(grenzen, len(kachel))
    verzeichnis = []
    daten = bytearray()
    for a, e in zip(grenzen[:-1], grenzen[1:]):
        if (p0[a:e] == KEINE_ANGABE).all():
            continue
        ebene = np.full(2 * KANTE * KANTE, KEINE_ANGABE, dtype=np.uint8)
        ebene[KANTE * KANTE:] = 0
        i = (z[a:e] % KANTE) * KANTE + s[a:e] % KANTE
        ebene[i] = p0[a:e]
        ebene[KANTE * KANTE + i] = zweites[a:e]
        gepackt = zlib.compress(ebene.tobytes(), 9)
        verzeichnis.append((int(z[a] // KANTE), int(s[a] // KANTE), len(daten), len(gepackt)))
        daten += gepackt
    anfang = KOPF + 16 * len(verzeichnis)
    kopf = struct.pack("<8siiiiii", MAGIC, VERSION, TEILUNG, KANTE, len(verzeichnis),
                       int(stand.replace("-", "")), quellen)
    pfad.parent.mkdir(parents=True, exist_ok=True)
    teil = pfad.with_name(pfad.name + ".teil")
    with open(teil, "wb") as f:
        f.write(kopf)
        for zk, sk, o, n in verzeichnis:
            f.write(struct.pack("<iiii", zk, sk, anfang + o, n))
        f.write(daten)
    teil.replace(pfad)
    return len(verzeichnis)


# ------------------------------------------------------------------ Prüfung


def pruefen(schluessel: np.ndarray, p0: np.ndarray, luecke: Path) -> None:
    """Vergleich mit licht-luecke.bin der Werkbank (amtliche Laserdaten, analyse.md 4.7b)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hoehen"))
    import hoehenmodell as hm  # noqa: PLC0415

    breite, laenge = mitte(schluessel)
    ref = np.full(len(schluessel), np.nan)
    for e in hm.lies(luecke):
        zeile = np.floor((e.nord - breite * 3600 * e.teilung) / e.schritt + 0.5).astype(np.int64)
        spalte = np.floor((laenge * 3600 * e.teilung - e.west) / e.schritt + 0.5).astype(np.int64)
        zeilen, spalten = e.hoehen.shape
        innen = (zeile >= 0) & (zeile < zeilen) & (spalte >= 0) & (spalte < spalten)
        ref[innen] = e.meter()[zeile[innen], spalte[innen]]
    gut = np.isfinite(ref) & (p0 != KEINE_ANGABE)
    ref, unser = ref[gut], p0[gut] / 100
    if not len(ref):
        print("Prüfung: keine gemeinsamen Zellen")
        return

    def klasse(p):
        return np.digitize(p, [0.3, 0.7])

    fehler = np.abs(ref - unser).mean()
    treffer = (klasse(ref) == klasse(unser)).mean()
    print(f"Prüfung gegen {luecke.name}: {len(ref)} Zellen, P₀ Referenz {ref.mean():.2f}, CHMv2 {unser.mean():.2f}, "
          f"mittlerer Fehler {fehler:.3f} (Ziel ≤ 0,12), Korrelation {np.corrcoef(ref, unser)[0, 1]:.2f}, "
          f"Klassen dunkel/mittel/hell gleich {treffer:.0%} (Ziel ≥ 80 %)")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--wege", type=Path, required=True)
    p.add_argument("--ausgabe", type=Path, required=True)
    p.add_argument("--blatttyp", type=Path, nargs="*", default=[], help="Copernicus-DLT-GeoTIFF(s)")
    p.add_argument("--zwischenspeicher", type=Path, help="Ordner für ganze CHMv2-Kacheln (werden danach gelöscht)")
    p.add_argument("--gebiet", help="süd,nord,west,ost in Grad (Standard: Kerngebiet)")
    p.add_argument("--stand", default=dt.date.today().isoformat())
    p.add_argument("--pruefen", type=Path, help="licht-luecke.bin der Werkbank zum Vergleich")
    a = p.parse_args()
    gebiet = KERN if not a.gebiet else tuple(map(float, a.gebiet.split(",")))
    beginn = time.time()

    b, l = wegpunkte(a.wege)
    schluessel = zellen(b, l, gebiet)
    del b, l
    print(f"{len(schluessel)} Wegzellen mit Nachbarn ({time.time() - beginn:.0f} s)", flush=True)

    summen = Summen(schluessel)
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
                      GDAL_HTTP_MAX_RETRY="5", GDAL_HTTP_RETRY_DELAY="2"):
        bloecke = chm_lesen(summen, a.zwischenspeicher)
    print(f"{bloecke} Blöcke CHMv2 gelesen ({time.time() - beginn:.0f} s)")

    quellen = QUELLE_CHM
    typ = None
    if a.blatttyp:
        typ = blatttyp(schluessel, a.blatttyp)
        quellen |= QUELLE_DLT
        mit = summen.krone > 0
        zaehl = np.bincount(typ[mit], minlength=4)
        print(f"Blatttyp in Zellen mit Krone: unbekannt {zaehl[0]}, Nadel {zaehl[1]}, Laub {zaehl[2]}, gemischt {zaehl[3]}")

    p0, zweites = werte(summen, typ)
    mit = p0 != KEINE_ANGABE
    print(f"P₀ in {mit.sum()} von {len(p0)} Zellen, Mittel {p0[mit].mean() / 100:.2f}, "
          f"dunkel (< 0,3) {np.mean(p0[mit] < 30):.0%}" if mit.any() else "keine Werte")
    if a.pruefen:
        pruefen(schluessel, p0, a.pruefen)
    kacheln = schreiben(a.ausgabe, schluessel, p0, zweites, a.stand, quellen)
    print(f"{a.ausgabe}: {kacheln} Kacheln, {a.ausgabe.stat().st_size / 1e6:.1f} MB, Stand {a.stand} "
          f"({time.time() - beginn:.0f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
