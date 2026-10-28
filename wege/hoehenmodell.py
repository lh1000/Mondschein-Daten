"""Erzeugt das Höhenmodell der App.

Zwei Ebenen in einer Datei (Format: siehe README.md):

- Kern:   mit --amtlich 1/3″ (ca. 10 m × 7 m) aus den amtlichen Laserscan-Geländemodellen von BEV
          und Bayern (Mittelwert je Zelle, Dezimeter, siehe amtlich.py); sonst 1″ aus Copernicus
- Puffer: ca. 150 km rundherum, 3″ (ca. 90 m), MAXIMUM je Zelle, damit Grate nicht abgeflacht
          werden (docs/mathe.md, Abschnitt 2.1). Grundlage Copernicus DEM GLO-30; mit --amtlich
          im Kerngebiet aus dem Kern und in Österreich aus dem BEV-Geländemodell

Aufruf:
    python hoehenmodell.py --amtlich --ausgabe <datei>
"""

from __future__ import annotations

import argparse
import math
import os
import struct
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from numpy.lib.format import open_memmap
from scipy.ndimage import map_coordinates

BOGENSEK_PRO_GRAD = 3600
KEINE_DATEN = -32768
KACHEL = 256
MAGIC = b"MSHOEHE2"
VERSION = 2
BESCHREIBUNG = 64  # Byte je Ebene in Version 2 (Version 1: 48)
ARBEITSORDNER = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Mondschein"
QUELLE = "https://copernicus-dem-30m.s3.amazonaws.com/{name}/{name}.tif"


@dataclass(frozen=True)
class Gebiet:
    """Rechteck in Grad; Ränder werden auf das Raster der Ebene gerundet."""

    sued: float
    nord: float
    west: float
    ost: float


# Kerngebiet: Nördliche Kalkalpen von den Ammergauer Alpen bis zu den Berchtesgadenern,
# inklusive Inntal/Nordkette im Süden und Alpenrand im Norden (Feinabgrenzung Meilenstein 4).
KERN = Gebiet(sued=47.25, nord=47.90, west=10.70, ost=13.15)

# Horizontpuffer: ca. 150 km um das Kerngebiet. 1° Breite ≈ 111 km; 1° Länge ist am
# Nordrand des Puffers am kürzesten, dort wird gerechnet, damit überall 150 km erreicht werden.
PUFFER_KM = 150.0
_PUFFER_NORD = KERN.nord + PUFFER_KM / 111.2
_GRAD_LAENGE_KM = 111.2 * math.cos(math.radians(_PUFFER_NORD))
PUFFER = Gebiet(
    sued=KERN.sued - PUFFER_KM / 111.2,
    nord=_PUFFER_NORD,
    west=KERN.west - PUFFER_KM / _GRAD_LAENGE_KM,
    ost=KERN.ost + PUFFER_KM / _GRAD_LAENGE_KM,
)


def kachelname(lat: int, lon: int) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"


class Mosaik:
    """Liest beliebige Ausschnitte aus den 1°-Kacheln, Pixelmitten auf ganzen Bogensekunden.

    Kachel N47/E011 enthält die Mitten 48°00′00″ (Zeile 0) bis 47°00′01″ (Zeile 3599) und
    11°00′00″ (Spalte 0) bis 11°59′59″ (Spalte 3599). Ab 50° Breite hat Copernicus nur 2400 Spalten
    (1,5″); sie werden auf ganze Bogensekunden gebracht (nächster Wert).
    """

    def __init__(self, cache: Path):
        self.cache = cache
        self.cache.mkdir(parents=True, exist_ok=True)
        self._kacheln: dict[tuple[int, int], np.ndarray | None] = {}

    def _kachel(self, lat: int, lon: int) -> np.ndarray | None:
        schluessel = (lat, lon)
        if schluessel not in self._kacheln:
            self._kacheln.clear()  # höchstens eine Kachel (50 MB) im Speicher
            self._kacheln[schluessel] = self._lade(lat, lon)
        return self._kacheln[schluessel]

    def _lade(self, lat: int, lon: int) -> np.ndarray | None:
        name = kachelname(lat, lon)
        pfad = self.cache / f"{name}.tif"
        if not pfad.exists():
            print(f"  lade {name}")
            try:
                urllib.request.urlretrieve(QUELLE.format(name=name), pfad.with_suffix(".part"))
            except urllib.error.HTTPError as fehler:
                if fehler.code in (403, 404):  # reine Meeresflächen gibt es nicht
                    return None
                raise
            pfad.with_suffix(".part").rename(pfad)
        with rasterio.open(pfad) as d:
            if d.height != 3600 or d.width not in (3600, 2400) or abs(d.transform.c - (lon - 0.5 / d.width)) > 1e-9:
                raise ValueError(f"Unerwartete Rastergeometrie in {name}")
            daten = d.read(1)
        if daten.shape[1] == 2400:
            daten = daten[:, (2 * np.arange(3600) + 1) // 3]  # Mitte s″ → Spalte s / 1,5
        return daten

    def ausschnitt(self, nord: int, sued: int, west: int, ost: int) -> np.ndarray:
        """Höhen (float32, NaN = keine Daten) für Mitten nord..sued × west..ost in Bogensekunden."""
        ergebnis = np.full((nord - sued + 1, ost - west + 1), np.nan, dtype=np.float32)
        lat_kacheln = range(math.ceil(sued / 3600) - 1, math.ceil(nord / 3600))
        lon_kacheln = range(math.floor(west / 3600), math.floor(ost / 3600) + 1)
        for lat in lat_kacheln:
            k_nord, k_sued = (lat + 1) * 3600, lat * 3600 + 1
            n, s = min(nord, k_nord), max(sued, k_sued)
            if n < s:
                continue
            for lon in lon_kacheln:
                k_west, k_ost = lon * 3600, lon * 3600 + 3599
                w, o = max(west, k_west), min(ost, k_ost)
                if w > o:
                    continue
                daten = self._kachel(lat, lon)
                if daten is None:
                    continue
                ergebnis[nord - n : nord - s + 1, w - west : o - west + 1] = daten[
                    k_nord - n : k_nord - s + 1, w - k_west : o - k_west + 1
                ]
        return ergebnis


@dataclass
class Ebene:
    nord: int  # Mitte der ersten Zeile, Rastereinheiten (1/teilung Bogensekunde)
    west: int  # Mitte der ersten Spalte, Rastereinheiten
    schritt: int  # Rasterweite, Rastereinheiten
    hoehen: np.ndarray  # int16 (auch np.memmap), Meter · faktor, KEINE_DATEN für Lücken
    teilung: int = 1  # Rastereinheiten je Bogensekunde
    faktor: int = 1  # gespeicherte Werte je Meter (10 = Dezimeter)

    @property
    def schritt_bogensek(self) -> float:
        return self.schritt / self.teilung

    def meter(self, zeilen: slice = slice(None), spalten: slice = slice(None)) -> np.ndarray:
        """Ausschnitt in Metern (float32), Lücken als NaN."""
        roh = np.asarray(self.hoehen[zeilen, spalten])
        return np.where(roh == KEINE_DATEN, np.nan, roh / np.float32(self.faktor)).astype(np.float32)


def raster(gebiet: Gebiet, schritt: int, teilung: int = 1) -> tuple[int, int, int, int]:
    """Ränder des Gebiets in Rastereinheiten als Vielfache des Schritts, nach außen gerundet."""
    eps = 1e-6  # 13.15 · 3600 ist in Gleitkomma nicht exakt 47340
    e = 3600 * teilung
    nord = math.ceil(gebiet.nord * e / schritt - eps) * schritt
    sued = math.floor(gebiet.sued * e / schritt + eps) * schritt
    west = math.floor(gebiet.west * e / schritt + eps) * schritt
    ost = math.ceil(gebiet.ost * e / schritt - eps) * schritt
    return nord, sued, west, ost


def als_int16(hoehen: np.ndarray, faktor: int = 1) -> np.ndarray:
    """Meter → gespeicherte Werte (Meter · faktor); NaN → KEINE_DATEN."""
    ergebnis = np.full(hoehen.shape, KEINE_DATEN, dtype=np.int16)
    gueltig = np.isfinite(hoehen)
    grenze = min(9000, 32767 // faktor)
    ergebnis[gueltig] = np.rint(np.clip(hoehen[gueltig], -500, grenze) * faktor).astype(np.int16)
    return ergebnis


def kern_ebene(mosaik: Mosaik) -> Ebene:
    nord, sued, west, ost = raster(KERN, 1)
    print(f"Kern: {(nord - sued) // 1 + 1} × {(ost - west) // 1 + 1} Punkte (1″)")
    return Ebene(nord, west, 1, als_int16(mosaik.ausschnitt(nord, sued, west, ost)))


def puffer_ebene(mosaik: Mosaik, schritt: int = 3, gebiet: Gebiet = PUFFER) -> Ebene:
    """Maximum je schritt×schritt-Block um die Mitte des groben Rasters."""
    nord, sued, west, ost = raster(gebiet, schritt)
    zeilen = (nord - sued) // schritt + 1
    spalten = (ost - west) // schritt + 1
    print(f"Puffer: {zeilen} × {spalten} Punkte ({schritt}″, Maximum)")
    halb = schritt // 2
    hoehen = np.empty((zeilen, spalten), dtype=np.int16)
    streifen = 200  # grobe Zeilen je Durchgang, begrenzt den Speicherbedarf
    for z0 in range(0, zeilen, streifen):
        z1 = min(zeilen, z0 + streifen)
        fein = mosaik.ausschnitt(
            nord - z0 * schritt + halb,
            nord - (z1 - 1) * schritt - halb,
            west - halb,
            ost + halb,
        )
        bloecke = fein.reshape(z1 - z0, schritt, spalten, schritt)
        with np.errstate(all="ignore"):
            maximum = np.nanmax(np.where(np.isnan(bloecke), -np.inf, bloecke), axis=(1, 3))
        maximum[np.isneginf(maximum)] = np.nan
        hoehen[z0:z1] = als_int16(maximum)
        print(f"  {z1}/{zeilen}", end="\r")
    print()
    return Ebene(nord, west, schritt, hoehen)


def schreibe(pfad: Path, ebenen: list[Ebene]) -> None:
    """Version 2, Kachelzeile für Kachelzeile – die Ebenen dürfen größer als der Arbeitsspeicher sein."""
    kopf_groesse = 16 + BESCHREIBUNG * len(ebenen)
    pfad.parent.mkdir(parents=True, exist_ok=True)
    teil = pfad.with_suffix(".part")
    beschreibungen = []
    with open(teil, "wb") as f:
        f.write(bytes(kopf_groesse))
        for e in ebenen:
            zeilen, spalten = e.hoehen.shape
            kz, ks = -(-zeilen // KACHEL), -(-spalten // KACHEL)
            daten_offset = f.tell()
            maxima = np.empty((kz, ks), np.int16)
            for tz in range(kz):
                streifen = np.full((KACHEL, ks * KACHEL), KEINE_DATEN, dtype=np.int16)
                stueck = np.asarray(e.hoehen[tz * KACHEL:(tz + 1) * KACHEL])
                streifen[:stueck.shape[0], :spalten] = stueck
                kacheln = streifen.reshape(KACHEL, ks, KACHEL).swapaxes(0, 1)  # [ks, 256, 256]
                maxima[tz] = kacheln.max(axis=(1, 2))
                f.write(np.ascontiguousarray(kacheln).astype("<i2").tobytes())
            maxima_offset = f.tell()
            f.write(maxima.astype("<i2").tobytes())
            maximum_m = -(-int(maxima.max()) // e.faktor)  # ganze Meter, aufgerundet
            beschreibungen.append(struct.pack(
                "<8iqqiiq", e.nord, e.west, e.schritt, e.teilung, zeilen, spalten, KACHEL, e.faktor,
                daten_offset, maxima_offset, maximum_m, 0, 0,
            ))
        f.seek(0)
        f.write(MAGIC + struct.pack("<ii", VERSION, len(ebenen)) + b"".join(beschreibungen))
    teil.replace(pfad)
    print(f"{pfad} geschrieben ({pfad.stat().st_size / 1e6:.0f} MB)")


def lies(pfad: Path, ordner: Path = ARBEITSORDNER / "zeilen") -> list[Ebene]:
    """Ebenen einer Höhendatei (Version 1 oder 2), feinste zuerst, als zeilenweise int16-Arrays.

    Die Datei speichert Kacheln; für Abfragen über ganze Zeilen wird jede Ebene einmalig
    umsortiert und als .npy zwischengespeichert. Mehrere Prozesse teilen sich diese Dateien
    über den Speicher des Betriebssystems (memory-mapped).
    """
    with open(pfad, "rb") as f:
        kopf = f.read(16)
    magic, (version, anzahl) = kopf[:8], struct.unpack_from("<ii", kopf, 8)
    if (magic, version) not in ((b"MSHOEHE1", 1), (b"MSHOEHE2", 2)):
        raise ValueError(f"Keine Mondschein-Höhendatei: {pfad}")
    groesse = 48 if version == 1 else 64
    roh = np.memmap(pfad, dtype=np.uint8, mode="r")
    ordner.mkdir(parents=True, exist_ok=True)
    stempel = f"{pfad.stat().st_size}_{int(pfad.stat().st_mtime)}"
    ebenen = []
    for n in range(anzahl):
        o = 16 + groesse * n
        if version == 1:
            nord, west, schritt, zeilen, spalten, kante, off, _, _, _ = struct.unpack_from("<6iqqii", roh, o)
            teilung, faktor = 1, 1
        else:
            nord, west, schritt, teilung, zeilen, spalten, kante, faktor, off, _, _, _, _ = (
                struct.unpack_from("<8iqqiiq", roh, o))
        zwischen = ordner / f"{pfad.stem}_{stempel}_{n}.npy"
        if not zwischen.exists():
            kz, ks = -(-zeilen // kante), -(-spalten // kante)
            kacheln = np.frombuffer(roh, "<i2", kz * ks * kante * kante, off).reshape(kz, ks, kante, kante)
            teil = zwischen.with_name(zwischen.stem + "_teil.npy")
            ziel = open_memmap(teil, mode="w+", dtype=np.int16, shape=(zeilen, spalten))
            for tz in range(kz):
                z0, z1 = tz * kante, min(zeilen, (tz + 1) * kante)
                ziel[z0:z1] = kacheln[tz].swapaxes(0, 1).reshape(kante, ks * kante)[:z1 - z0, :spalten]
            ziel.flush()
            del ziel
            teil.replace(zwischen)
        ebenen.append(Ebene(nord, west, schritt, np.load(zwischen, mmap_mode="r"), teilung, faktor))
    del roh
    return sorted(ebenen, key=lambda e: e.schritt_bogensek)


# Plausibilitätsprüfung: bekannte Gipfel. GLO-30 liegt an Gipfeln oft 10–30 m zu tief.
GIPFEL = {
    "Zugspitze": (47.42110, 10.98530, 2962),
    "Watzmann-Mittelspitze": (47.55470, 12.92180, 2713),
    "Wendelstein": (47.70360, 12.01280, 1838),
    "Großglockner (Puffer)": (47.07450, 12.69410, 3798),
}


def pruefe(ebenen: list[Ebene]) -> None:
    for name, (lat, lon, soll) in GIPFEL.items():
        for e in sorted(ebenen, key=lambda e: e.schritt_bogensek):
            z = round((e.nord - lat * 3600 * e.teilung) / e.schritt)
            s = round((lon * 3600 * e.teilung - e.west) / e.schritt)
            if 0 <= z < e.hoehen.shape[0] and 0 <= s < e.hoehen.shape[1]:
                # nächster Rasterpunkt und Maximum im Umkreis von ca. 60 m
                r = max(2, round(2 / e.schritt_bogensek))
                umfeld = e.meter(slice(max(0, z - r), z + r + 1), slice(max(0, s - r), s + r + 1))
                print(f"  {name}: Raster {e.meter(slice(z, z + 1), slice(s, s + 1))[0, 0]:.1f} m, "
                      f"Umfeld max {np.nanmax(umfeld):.1f} m, Soll {soll} m ({e.schritt_bogensek:.3g}″)")
                break


# ---------------------------------------------------------------- amtliche Daten

def kern_ebene_amtlich(mosaik: Mosaik, ordner: Path) -> Ebene:
    """Kern zu 1/3″ aus BEV ALS DTM und Bayern DGM1 (Mittelwert je Zelle), in Dezimetern."""
    import amtlich

    teilung, faktor = 3, 10
    nord, sued, west, ost = raster(KERN, 1, teilung)
    gitter = amtlich.Gitter(nord, west, 1, teilung, nord - sued + 1, ost - west + 1)
    print(f"Kern: {gitter.zeilen} × {gitter.spalten} Punkte (1/3″), Arbeitsordner {ordner}")
    summen = amtlich.Summen(ordner, "kern", gitter)
    amtlich.oesterreich(summen, aufloesung=2)
    amtlich.bayern(summen, ordner)
    hoehen = open_memmap(ordner / "kern_dm.npy", mode="w+", dtype=np.int16, shape=(gitter.zeilen, gitter.spalten))
    luecken = amtlich.mittelwert_in_bloecken(summen, hoehen, faktor)
    print(f"  Lücken nach BEV und Bayern: {luecken} Punkte – werden mit Copernicus gefüllt")
    e = Ebene(nord, west, 1, hoehen, teilung, faktor)
    fuelle_luecken(e, mosaik)
    hoehen.flush()
    return e


def fuelle_luecken(e: Ebene, mosaik: Mosaik, zeilen_je_block: int = 1536) -> None:
    """Datenlücken einer feinen Ebene bilinear aus dem 1″-Copernicus-Raster füllen."""
    gesamt = 0
    for z0 in range(0, e.hoehen.shape[0], zeilen_je_block):
        z1 = min(e.hoehen.shape[0], z0 + zeilen_je_block)
        block = np.asarray(e.hoehen[z0:z1]).copy()
        zz, ss = np.nonzero(block == KEINE_DATEN)
        if not len(zz):
            continue
        breite = (e.nord - (z0 + zz) * e.schritt) / e.teilung  # Bogensekunden
        laenge = (e.west + ss * e.schritt) / e.teilung
        n, s = math.ceil(breite.max()) + 1, math.floor(breite.min()) - 1
        w, o = math.floor(laenge.min()) - 1, math.ceil(laenge.max()) + 1
        werte = map_coordinates(mosaik.ausschnitt(n, s, w, o), [n - breite, laenge - w], order=1, cval=np.nan)
        block[zz, ss] = als_int16(werte, e.faktor)
        e.hoehen[z0:z1] = block
        gesamt += len(zz)
    print(f"  mit Copernicus gefüllt: {gesamt} Punkte")


def puffer_amtlich(puffer: Ebene, kern: Ebene, ordner: Path) -> None:
    """Puffer verbessern: in Österreich BEV-Maximum (8 m), im Kerngebiet Maximum der Kernpunkte."""
    import amtlich

    zeilen, spalten = puffer.hoehen.shape
    gitter = amtlich.Gitter(puffer.nord, puffer.west, puffer.schritt, 1, zeilen, spalten)
    summen = amtlich.Summen(ordner, "puffer_at", gitter, maximum=True)
    amtlich.oesterreich(summen, aufloesung=8)
    # Abdeckung = gezählte 8-m-Pixel / erwartete Anzahl je Zelle (Zellfläche / 64 m²)
    breite = np.radians((puffer.nord - np.arange(zeilen) * puffer.schritt) / 3600)
    zelle = (puffer.schritt * math.pi / 648000 * 6_380_000) ** 2 * np.cos(breite)
    ersetzt = gemischt = 0
    for z0 in range(0, zeilen, 500):
        z1 = min(zeilen, z0 + 500)
        abdeckung = np.asarray(summen.anzahl[z0:z1]) / (zelle[z0:z1, None] / 64)
        bev = np.asarray(summen.wert[z0:z1])
        bev = np.where(np.isfinite(bev), bev, np.nan)
        cop = puffer.meter(slice(z0, z1))
        # Die Pixelzahl je Zelle streut rundungsbedingt um ±5 % um 1; echte Datenränder liegen deutlich darunter.
        voll = (abdeckung >= 0.8) & np.isfinite(bev)
        teil = (abdeckung > 0.02) & ~voll & np.isfinite(bev)
        neu = np.where(voll, bev, np.where(teil, np.fmax(bev, cop), cop))
        puffer.hoehen[z0:z1] = als_int16(neu, puffer.faktor)
        ersetzt += int(voll.sum())
        gemischt += int(teil.sum())
    print(f"  Puffer: {ersetzt} Punkte aus BEV, {gemischt} am Datenrand mit Copernicus kombiniert")

    # Im Kerngebiet: Maximum der 9 × 9 Kernpunkte um jeden Pufferpunkt (3″ = 9 · 1/3″).
    v = puffer.schritt * kern.teilung // kern.schritt  # Kernpunkte je Pufferschritt
    h = v // 2
    zr = (kern.nord - (puffer.nord - np.arange(zeilen) * puffer.schritt) * kern.teilung) // kern.schritt
    sr = ((puffer.west + np.arange(spalten) * puffer.schritt) * kern.teilung - kern.west) // kern.schritt
    za = np.nonzero((zr - h >= 0) & (zr + h < kern.hoehen.shape[0]))[0]
    sa = np.nonzero((sr - h >= 0) & (sr + h < kern.hoehen.shape[1]))[0]
    s0, s1 = sa[0], sa[-1] + 1
    for b0 in range(za[0], za[-1] + 1, 100):
        b1 = min(za[-1] + 1, b0 + 100)
        k = np.asarray(kern.hoehen[zr[b0] - h: zr[b1 - 1] + h + 1, sr[s0] - h: sr[s1 - 1] + h + 1])
        bloecke = k.reshape(b1 - b0, v, s1 - s0, v).max(axis=(1, 3)).astype(np.float32)
        bloecke[bloecke == KEINE_DATEN] = np.nan
        puffer.hoehen[b0:b1, s0:s1] = als_int16(bloecke / kern.faktor, puffer.faktor)
    print(f"  Puffer im Kerngebiet aus dem Kern: {len(za)} × {s1 - s0} Punkte")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cache", type=Path, default=ARBEITSORDNER / "copernicus")
    parser.add_argument("--amtlich", action="store_true", help="Kern aus BEV und Bayern (Laserscan, 1/3″)")
    parser.add_argument("--arbeitsordner", type=Path, default=ARBEITSORDNER / "amtlich",
                        help="Zwischenstände für --amtlich (ca. 2 GB; ein Abbruch wird fortgesetzt)")
    parser.add_argument("--ausgabe", type=Path, required=True)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    print(f"Kern {KERN}\nPuffer {PUFFER}")
    mosaik = Mosaik(args.cache)
    if args.amtlich:
        kern = kern_ebene_amtlich(mosaik, args.arbeitsordner)
        puffer = puffer_ebene(mosaik)
        puffer_amtlich(puffer, kern, args.arbeitsordner)
        ebenen = [kern, puffer]
    else:
        ebenen = [kern_ebene(mosaik), puffer_ebene(mosaik)]
    for e in ebenen:
        bloecke = [np.asarray(e.hoehen[z:z + 1000]) for z in range(0, e.hoehen.shape[0], 1000)]
        luecken = sum(int((b == KEINE_DATEN).sum()) for b in bloecke)
        maximum = max(int(b.max()) for b in bloecke)
        print(f"Ebene {e.schritt_bogensek:.3g}″: max {maximum / e.faktor:.1f} m, Lücken {luecken}")
    pruefe(ebenen)
    schreibe(args.ausgabe, ebenen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
