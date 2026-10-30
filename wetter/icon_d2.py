"""Wolkendatei aus dem DWD-Modell ICON-D2 für die App (docs/konzept.md, Meilenstein 14).

Lädt einen Modelllauf von opendata.dwd.de (3D-Wolkenanteil `clc` auf Modellflächen und deren Höhen
`hhl`), bringt ihn auf das 0,02°-Gitter, schneidet das Gebiet aus und schreibt eine kompakte Datei, aus
der die App die Wolken entlang der Sichtlinie offline liest (docs/mathe.md, Abschnitt 10).

Quellen (--quelle):
    v1   neues URL-Schema weather/nwp/v1/m/icon-d2, nur das ICON-Dreiecksgitter (542 040 Zellen).
         Jeder Knoten des 0,02°-Gitters nimmt den Wert der nächsten Dreieckszelle (Lage aus CLAT/CLON
         des Laufs); weiter als 3 km von jeder Zelle: fehlt. Seit 30.10.2026, weil der DWD das alte
         Schema nach dem 30.11.2026 abschaltet.
    alt  (Standard bis zur Prüfung) bisheriges Schema weather/nwp/icon-d2/grib, regelmäßiges 0,02°-Gitter (bz2).

Aufruf:
    python icon_d2.py --ziel ..\\..\\..\\Daten\\wolken\\mondschein-wolken.bin [--lauf 2026102606] [--stunden 48]

Ohne --lauf wird der neueste vollständige Lauf genommen. Braucht eccodes (pip install eccodes).
Daten: Deutscher Wetterdienst, Lizenz CC BY 4.0 (Quelle: Deutscher Wetterdienst).

Dateiformat „MSWOLKE1“ (little endian), Zeile 0 jeder Ebene im Süden:
    8 B   Kennung b"MSWOLKE1"
    i64   Start des Modelllaufs (Unix-Sekunden, UTC)
    i32   Anzahl Stunden H (Vorhersagestunde 0 … H-1, stündlich)
    i32   Anzahl Flächen L (von unten nach oben)
    i32   Anzahl Ebenen E
    je Ebene 5 × i32: i0, j0 (Index der Südwest-Zelle im globalen 0,02°-Gitter: round(Breite/0,02),
                       round(Länge/0,02)), Zeilen, Spalten, Schritt (1 = 0,02°, 2 = 0,04°)
    Inhaltsverzeichnis: je Block i64 Versatz, i32 Länge – erst E Höhenblöcke, dann H × E Wolkenblöcke
    Blöcke zlib-komprimiert:
      Höhe:  i16 [Zeile][Spalte][Fläche] Höhe der Flächenmitte in m über dem Meeresspiegel, je Zelle
             die unterste Fläche absolut, darüber der Abstand zur Fläche darunter (packt 2,5-mal besser)
      Wolken: u8 [Zeile][Spalte][Fläche] Wolkenanteil in %, auf 5 % gerundet (packt 1,5-mal besser),
             255 = fehlt
"""

from __future__ import annotations

import argparse
import bz2
import datetime as dt
import hashlib
import io
import json
import math
import re
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import eccodes
import numpy as np

QUELLE = "https://opendata.dwd.de/weather/nwp/icon-d2/grib"
QUELLE_V1 = "https://opendata.dwd.de/weather/nwp/v1/m/icon-d2/p"

# Dreiecksgitter: weiter als so weit (km) von der nächsten Zelle gilt ein Gitterknoten als außerhalb
NAECHSTE_MAX_KM = 3.0
KENNUNG = b"MSWOLKE1"

# Globales 0,02°-Gitter, wie Wettergitter in core/ (Index = round(Grad / 0,02))
WEITE = 0.02

# Gebiet: fein (0,02°) um das Gesamtgebiet mit ca. 60 km Rand, grob (0,04°) bis ca. 200 km
# (Horizontpuffer, docs/konzept.md Abschnitt 7). Seit der Gebietserweiterung (Meilenstein 30) in
# Kacheln: fein 1° × 1°, grob 2° × 2° (nur außerhalb des feinen Gebiets); die App lädt per HTTP-Range
# nur die Kacheln um ihre Regionen (core: Wolkenausschnitt). Gesamtgebiete aus tools/gebiete/gebiete.json.
GEBIETE = Path(__file__).resolve().parent.parent / "gebiete" / "gebiete.json"
RAND_FEIN = 0.6  # Grad
RAND_GROB = 2.5

# Flächen bis knapp über die Wolkenobergrenze der Sichtlinie (Sichtlinie.OBERGRENZE = 12 km)
OBERGRENZE = 12_500.0

FEHLT = 255

# Wolkenanteil in 5-%-Stufen: Fehler höchstens 2,5 %, gegenüber den Schwellen 30/70 % der Anzeige unerheblich
STUFE = 5
FEHLWERT = 1e20

# eccodes ist nicht threadsicher: herunterladen parallel, entschlüsseln nacheinander
ENTSCHLUESSELN = threading.Lock()


def laden(url: str, versuche: int = 4, warten_404: int = 10) -> bytes:
    """Lädt eine Datei. Fehlt sie (404), wartet es bis zu warten_404 Minuten: Der DWD lädt die Dateien
    eines Laufs nicht der Reihe nach hoch, die letzte Stunde kann vor einzelnen früheren da sein."""
    n = fehlt = 0
    while True:
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as fehler:
            if fehler.code != 404 or fehlt >= warten_404:
                if fehler.code == 404 or n >= versuche - 1:
                    raise
                n += 1
                time.sleep(2 * n)
                continue
            fehlt += 1
            time.sleep(60)
        except Exception:
            if n >= versuche - 1:
                raise
            n += 1
            time.sleep(2 * n)


def grib_feld(daten: bytes) -> tuple[np.ndarray, float, float]:
    """Entpackt eine GRIB2-Datei (bz2): Werte als Feld [Zeile von Süd][Spalte von West], Südwest-Ecke."""
    nachricht = bz2.decompress(daten)
    with ENTSCHLUESSELN:
        return _entschluesseln(nachricht)


def _entschluesseln(nachricht: bytes) -> tuple[np.ndarray, float, float]:
    g = eccodes.codes_new_from_message(nachricht)
    try:
        ni = eccodes.codes_get(g, "Ni")
        nj = eccodes.codes_get(g, "Nj")
        lat0 = eccodes.codes_get(g, "latitudeOfFirstGridPointInDegrees")
        lon0 = eccodes.codes_get(g, "longitudeOfFirstGridPointInDegrees")
        nach_norden = eccodes.codes_get(g, "jScansPositively") == 1
        # Fehlende Werte kennzeichnen; der Standard 9999 wäre auch eine echte Höhe der oberen Flächen
        eccodes.codes_set(g, "missingValue", FEHLWERT)
        werte = eccodes.codes_get_values(g).reshape(nj, ni)
    finally:
        eccodes.codes_release(g)
    if lon0 > 180:
        lon0 -= 360
    if not nach_norden:
        werte = werte[::-1]
        lat0 = lat0 - (nj - 1) * WEITE
    return werte, lat0, lon0


def neuester_lauf() -> str:
    """Neuester Lauf, dessen letzte Stunde schon vollständig bereitliegt (z. B. "2026102606")."""
    kandidaten = []
    for hh in range(0, 24, 3):
        try:
            text = laden(f"{QUELLE}/{hh:02d}/clc/", warten_404=0).decode()
        except Exception:
            continue
        for lauf in set(re.findall(r"model-level_(\d{10})_048_65_clc", text)):
            kandidaten.append(lauf)
    if not kandidaten:
        sys.exit("Kein vollständiger ICON-D2-Lauf gefunden")
    return max(kandidaten)


def datei_url(lauf: str, groesse: str, stunde: int, flaeche: int) -> str:
    hh = lauf[8:10]
    art = "time-invariant" if groesse == "hhl" else "model-level"
    return f"{QUELLE}/{hh}/{groesse}/icon-d2_germany_regular-lat-lon_{art}_{lauf}_{stunde:03d}_{flaeche}_{groesse}.grib2.bz2"


def v1_lauf(lauf: str) -> str:
    """„2026103006“ → Ordnername des Laufs im neuen Schema („2026-10-30T06%3A00“)."""
    return f"{lauf[0:4]}-{lauf[4:6]}-{lauf[6:8]}T{lauf[8:10]}%3A00"


def v1_url(param: str, lauf: str, stunde: int, flaeche: int | None) -> str:
    ebene = f"/lvt1/150/lv1/{flaeche}" if flaeche is not None else ""
    return f"{QUELLE_V1}/{param}{ebene}/r/{v1_lauf(lauf)}/s/PT{stunde:03d}H00M.grib2"


def grib_werte(daten: bytes) -> np.ndarray:
    """Werte einer GRIB2-Nachricht ohne Gitterangaben (Dreiecksgitter), fehlende als FEHLWERT."""
    with ENTSCHLUESSELN:
        g = eccodes.codes_new_from_message(daten)
        try:
            eccodes.codes_set(g, "missingValue", FEHLWERT)
            return eccodes.codes_get_values(g)
        finally:
            eccodes.codes_release(g)


def v1_neuester_lauf() -> str:
    """Neuester Lauf im neuen Schema, dessen Stunde 48 der untersten Fläche schon bereitliegt."""
    text = laden(f"{QUELLE_V1}/CLC/lvt1/150/lv1/65/r/", warten_404=0).decode()
    laeufe = sorted(set(re.findall(r'href="(\d{4})-(\d{2})-(\d{2})T(\d{2})%3A00/"', text)), reverse=True)
    for j, m, t, h in laeufe:
        lauf = f"{j}{m}{t}{h}"
        try:
            liste = laden(f"{QUELLE_V1}/CLC/lvt1/150/lv1/65/r/{v1_lauf(lauf)}/s/", warten_404=0).decode()
        except Exception:
            continue
        if "PT048H00M.grib2" in liste:
            return lauf
    sys.exit("Kein vollständiger ICON-D2-Lauf gefunden (neues Schema)")


class Dreiecksgitter:
    """Lage der ICON-D2-Zellen eines Laufs (CLAT/CLON) und je Ebene der Index der nächsten Zelle je Knoten."""

    def __init__(self, lauf: str):
        from scipy.spatial import cKDTree
        b = grib_werte(laden(v1_url("CLAT", lauf, 0, None)))
        l = grib_werte(laden(v1_url("CLON", lauf, 0, None)))
        # CLAT/CLON in Grad; falls in Radiant geliefert, umrechnen
        if np.nanmax(np.abs(b)) <= math.pi:
            b, l = np.degrees(b), np.degrees(l)
        self.b0 = math.radians(float(np.mean(b)))
        self.baum = cKDTree(self._xy(b, l))
        self.anzahl = len(b)

    def _xy(self, b: np.ndarray, l: np.ndarray) -> np.ndarray:
        return np.column_stack([b * 111.32, l * 111.32 * math.cos(self.b0)])

    def index(self, e: "Ebene") -> np.ndarray:
        """Index der nächsten Zelle je 0,02°-Knoten der Ebene, −1 wenn weiter als NAECHSTE_MAX_KM."""
        b, l = e.knoten()
        d, i = self.baum.query(self._xy(b.ravel(), l.ravel()))
        i = np.where(d <= NAECHSTE_MAX_KM, i, -1)
        return i.reshape(b.shape)


def auf_ebene(werte: np.ndarray, index: np.ndarray) -> np.ndarray:
    """Werte des Dreiecksgitters auf die Knoten einer Ebene (fehlende Zellen als FEHLWERT)."""
    aus = np.where(index >= 0, werte[np.clip(index, 0, None)], FEHLWERT)
    return aus


class Ebene:
    """Ausschnitt des Gitters mit Schritt 1 (0,02°) oder 2 (0,04°), ausgerichtet am globalen Gitter."""

    def __init__(self, grenzen: tuple[float, float, float, float], schritt: int):
        sued, nord, west, ost = grenzen
        self.schritt = schritt
        self.i0 = int(round(sued / WEITE)) // schritt * schritt
        self.j0 = int(round(west / WEITE)) // schritt * schritt
        self.zeilen = (int(round(nord / WEITE)) - self.i0) // schritt + 1
        self.spalten = (int(round(ost / WEITE)) - self.j0) // schritt + 1

    def knoten(self) -> tuple[np.ndarray, np.ndarray]:
        """Breite und Länge (Grad) aller 0,02°-Knoten der Ebene, [Zeile][Spalte] (bei Schritt 2 alle 2 × 2)."""
        s = self.schritt
        b = (self.i0 + np.arange(self.zeilen * s)) * WEITE
        l = (self.j0 + np.arange(self.spalten * s)) * WEITE
        return np.meshgrid(b, l, indexing="ij")

    def ausschneiden(self, feld: np.ndarray, lat0: float, lon0: float, mittel: bool) -> np.ndarray:
        """Ausschnitt aus dem DWD-Feld; bei Schritt 2 Mittel über 2 × 2 Zellen (fehlende Werte als NaN)."""
        di = self.i0 - int(round(lat0 / WEITE))
        dj = self.j0 - int(round(lon0 / WEITE))
        s = self.schritt
        teil = feld[di:di + self.zeilen * s, dj:dj + self.spalten * s].astype(np.float64)
        if teil.shape != (self.zeilen * s, self.spalten * s):
            sys.exit("Gebiet liegt nicht ganz im ICON-D2-Gitter")
        return self.zusammenfassen(teil, mittel)

    def zusammenfassen(self, teil: np.ndarray, mittel: bool) -> np.ndarray:
        """Feld auf den 0,02°-Knoten der Ebene → Zellen der Ebene (bei Schritt 2 Mittel bzw. erster Knoten)."""
        s = self.schritt
        teil = teil.astype(np.float64)
        teil[teil >= FEHLWERT] = np.nan
        if s == 1:
            return teil
        teil = teil.reshape(self.zeilen, s, self.spalten, s)
        return np.nanmean(teil, axis=(1, 3)) if mittel else teil[:, 0, :, 0]


def kacheln() -> list["Ebene"]:
    """Feine Kacheln um jedes Gesamtgebiet, dann grobe Kacheln bis 200 km, soweit nicht ganz fein."""
    gesamt = json.loads(GEBIETE.read_text(encoding="utf-8"))["gesamt"]
    gesamt = gesamt if isinstance(gesamt, list) else [gesamt]

    def rechtecke(rand: float, kante: int) -> set[tuple[int, int]]:
        r = set()
        for g in gesamt:
            for b in range(math.floor((g["sued"] - rand) / kante) * kante, math.ceil(g["nord"] + rand), kante):
                for l in range(math.floor((g["west"] - rand) / kante) * kante, math.ceil(g["ost"] + rand), kante):
                    r.add((b, l))
        return r

    fein = sorted(rechtecke(RAND_FEIN, 1))
    grob = sorted(k for k in rechtecke(RAND_GROB, 2)
                  if not all((k[0] + db, k[1] + dl) in fein for db in (0, 1) for dl in (0, 1)))
    # Grenzen so, dass benachbarte Kacheln lückenlos und ohne Überlappung aneinanderstoßen
    return ([Ebene((b, b + 1 - WEITE, l, l + 1 - WEITE), 1) for b, l in fein]
            + [Ebene((b, b + 2 - 2 * WEITE, l, l + 2 - 2 * WEITE), 2) for b, l in grob])


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ziel", type=Path)
    p.add_argument("--nur-lauf", action="store_true", help="nur den neuesten vollständigen Lauf ausgeben")
    p.add_argument("--json", type=Path, help="zusätzlich eine Beschreibung (Lauf, Größe, SHA-256) schreiben")
    p.add_argument("--lauf", help="z. B. 2026102606; ohne Angabe der neueste vollständige Lauf")
    p.add_argument("--stunden", type=int, default=48, help="letzte Vorhersagestunde (ICON-D2: bis 48)")
    p.add_argument("--parallel", type=int, default=16)
    p.add_argument("--quelle", choices=("v1", "alt"), default="alt", help="URL-Schema des DWD (alt endet nach dem 30.11.2026)")
    a = p.parse_args()

    beginn = time.time()
    v1 = a.quelle == "v1"
    lauf = a.lauf or (v1_neuester_lauf() if v1 else neuester_lauf())
    if a.nur_lauf:
        print(lauf)
        return
    if a.ziel is None:
        p.error("--ziel fehlt")
    print(f"Lauf {lauf} (Quelle {a.quelle})", flush=True)
    ebenen = kacheln()
    print(f"{len(ebenen)} Kacheln", flush=True)
    pool = ThreadPoolExecutor(a.parallel)

    if v1:
        gitter = Dreiecksgitter(lauf)
        indizes = [gitter.index(e) for e in ebenen]
        fehlend = sum(int((i < 0).sum()) for i in indizes)
        print(f"Dreiecksgitter: {gitter.anzahl} Zellen, {fehlend} Knoten außerhalb", flush=True)

        def feld(param: str, t: int, k: int):
            return grib_werte(laden(v1_url(param, lauf, t, k)))

        def ausschnitt(f, n: int, e: Ebene, mittel: bool) -> np.ndarray:
            return e.zusammenfassen(auf_ebene(f, indizes[n]), mittel)
    else:
        def feld(param: str, t: int, k: int):
            return grib_feld(laden(datei_url(lauf, param.lower(), t, k)))

        def ausschnitt(f, n: int, e: Ebene, mittel: bool) -> np.ndarray:
            return e.ausschneiden(f[0], f[1], f[2], mittel)

    # Höhen der 66 Halbflächen (feste Modellgeometrie); Flächenmitte = Mittel zweier Halbflächen
    hhl = dict(zip(range(1, 67), pool.map(lambda k: feld("HHL", 0, k), range(1, 67))))
    alle_flaechen = []
    for k in range(65, 0, -1):  # von unten nach oben
        mitte = [(ausschnitt(hhl[k], n, e, True) + ausschnitt(hhl[k + 1], n, e, True)) / 2 for n, e in enumerate(ebenen)]
        # Fehlen die Höhen (oberste Flächen) oder liegt die Fläche überall über der Wolkenobergrenze: fertig
        if any(np.isnan(m).all() for m in mitte) or min(np.nanmin(m) for m in mitte) > OBERGRENZE:
            break
        alle_flaechen.append((k, mitte))
    flaechen = [k for k, _ in alle_flaechen]
    print(f"{len(flaechen)} Flächen (Modellfläche {flaechen[0]} … {flaechen[-1]})")
    hoehenbloecke = []
    for n, e in enumerate(ebenen):
        h = np.stack([m[n] for _, m in alle_flaechen], axis=-1)  # [Zeile][Spalte][Fläche]
        h = np.nan_to_num(h, nan=-1).round().astype(np.int32)
        abstaende = np.diff(h, axis=2, prepend=0).astype("<i2")
        hoehenbloecke.append(zlib.compress(abstaende.tobytes(), 9))

    stunden = list(range(0, a.stunden + 1))

    def stunde_laden(t: int) -> list[bytes]:
        felder = list(pool_innen.map(lambda k: feld("CLC", t, k), flaechen))
        bloecke = []
        for n, e in enumerate(ebenen):
            w = np.stack([ausschnitt(f, n, e, True) for f in felder], axis=-1)
            u8 = np.where(np.isnan(w), FEHLT, np.clip(np.round(w / STUFE) * STUFE, 0, 100)).astype(np.uint8)
            bloecke.append(zlib.compress(u8.tobytes(), 9))
        print(f"  Stunde {t:2d} fertig", flush=True)
        return bloecke

    pool_innen = pool
    wolkenbloecke = [b for t in stunden for b in stunde_laden(t)]

    # Datei schreiben: Kopf, Inhaltsverzeichnis, Blöcke
    lauf_zeit = dt.datetime.strptime(lauf, "%Y%m%d%H").replace(tzinfo=dt.timezone.utc)
    kopf = io.BytesIO()
    kopf.write(KENNUNG)
    kopf.write(struct.pack("<qiii", int(lauf_zeit.timestamp()), len(stunden), len(flaechen), len(ebenen)))
    for e in ebenen:
        kopf.write(struct.pack("<iiiii", e.i0, e.j0, e.zeilen, e.spalten, e.schritt))
    bloecke = hoehenbloecke + wolkenbloecke
    versatz = kopf.tell() + len(bloecke) * 12
    for b in bloecke:
        kopf.write(struct.pack("<qi", versatz, len(b)))
        versatz += len(b)
    a.ziel.parent.mkdir(parents=True, exist_ok=True)
    teil = a.ziel.with_suffix(".part")
    with open(teil, "wb") as f:
        f.write(kopf.getvalue())
        for b in bloecke:
            f.write(b)
    teil.replace(a.ziel)
    groesse = a.ziel.stat().st_size
    if a.json:
        a.json.write_text(json.dumps({
            "lauf": lauf,
            "lauf_utc": lauf_zeit.isoformat(),
            "stunden": len(stunden),
            "datei": a.ziel.name,
            "groesse": groesse,
            "sha256": hashlib.sha256(a.ziel.read_bytes()).hexdigest(),
        }, indent=2), encoding="utf-8")
    je_stunde = sum(len(b) for b in wolkenbloecke) / len(stunden)
    print(f"{a.ziel}: {groesse / 1e6:.1f} MB, je Stunde {je_stunde / 1e3:.0f} kB, Höhen "
          f"{sum(len(b) for b in hoehenbloecke) / 1e6:.1f} MB, {time.time() - beginn:.0f} s")


if __name__ == "__main__":
    main()
