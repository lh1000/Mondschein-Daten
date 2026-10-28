"""Wegenetz für die Wanderplanung (docs/konzept.md, Stufe 3, Meilenstein 15).

Liest Geofabrik-Extrakte (.osm.pbf) und schreibt `mondschein-wege.bin`: alle Wege, Steige,
Forst- und Nebenstraßen im Kerngebiet als Graph (Knoten = Kreuzungen und Wegenden), je Kante
Länge, Auf- und Abstieg aus dem Höhenmodell der App, Wegart, SAC-Stufe und Sichtbarkeit, dazu die
vereinfachte Geometrie und Startpunkte (Parkplätze, Haltestellen, Bahnhöfe, Hütten, Seilbahnstationen).
Format: tools/wege/README.md; Leser in core: `io.github.lh1000.mondschein.core.wege.Wegenetz`.

Kartendaten © OpenStreetMap-Mitwirkende, ODbL 1.0.

Aufruf (Python-Umgebung aus tools/hoehen, siehe README):
    python wegenetz.py --pbf oberbayern-latest.osm.pbf austria-latest.osm.pbf
                       --hoehen ..\\..\\..\\Daten\\mondschein-hoehen.bin --ausgabe ..\\..\\..\\Daten\\mondschein-wege.bin
Für eine Region der Gebietserweiterung (Meilenstein 29) mit Höhenkacheln und ca. 25 km Rand:
    python wegenetz.py --pbf … --hoehen mondschein-hoehen-N47E009.bin … --gebiet 46.55,48.15,9.25,10.95
                       --ausgabe mondschein-wege-allgaeu-vorarlberg.bin
Für die Testdatei in core (kleiner Ausschnitt am Wendelstein):
    python wegenetz.py … --gebiet 47.665,47.71,11.99,12.04 --ausgabe ..\\..\\core\\src\\test\\resources\\wege-wendelstein.bin
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import struct
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import osmium
from scipy.ndimage import map_coordinates
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hoehen"))

import hoehenmodell as hm  # noqa: E402

MAGIC = b"MSWEGE1\0"
VERSION = 1
KOPF = 64
MIKRO = 1_000_000  # Knoten und Startpunkte in Mikrograd
GEO = 100_000  # Geometrie in 1/100 000 Grad (ca. 1,1 m Breite, 0,75 m Länge)
VEREINFACHUNG = 0.1  # Meter: nur Punkte (fast) auf einer Geraden entfallen; gröber kürzt Kehren und Stufen
HOEHENSCHRITT = 20.0  # Meter: Abstand der Höhenabfragen für Auf- und Abstieg
FANG = 300.0  # Meter: Startpunkte weiter weg vom Wegenetz werden verworfen
BERGBAHN_NAH = 60.0  # Meter: Bahnhalte und Bahnsteige so nah an einer Zahnrad- oder Standseilbahn sind Bergbahnhalte
ERDRADIUS = 6_371_000.0

# Wegart (Byte in der Datei, muss zu core `Wegart` passen)
WEGART = {"path": 0, "footway": 1, "track": 2, "steps": 3, "bridleway": 4, "via_ferrata": 5, "pedestrian": 6}
STRASSEN = {"unclassified", "service", "residential", "living_street", "tertiary"}
STRASSE = 7
SAC = {"hiking": 1, "mountain_hiking": 2, "demanding_mountain_hiking": 3,
       "alpine_hiking": 4, "demanding_alpine_hiking": 5, "difficult_alpine_hiking": 6}
SICHT = {"excellent": 1, "good": 2, "intermediate": 3, "bad": 4, "horrible": 5, "no": 6}
# Startpunkt-Art (Byte, muss zu core `Startart` passen)
PARKPLATZ, HALTESTELLE, BAHNHOF, HUETTE, SEILBAHN = range(5)
FUSS_JA = {"yes", "designated", "permissive"}


@dataclass
class Weg:
    knoten: np.ndarray  # OSM-Knoten-IDs
    breite: np.ndarray  # Grad
    laenge: np.ndarray
    art: int
    sac: int
    sicht: int
    name: str


def begehbar(tags) -> int | None:
    """Wegart, wenn der Weg zu Fuß benutzbar ist, sonst None."""
    h = tags.get("highway")
    if h in WEGART:
        art = WEGART[h]
    elif h in STRASSEN:
        if h == "service" and tags.get("service") in ("parking_aisle", "driveway", "drive-through"):
            return None
        art = STRASSE
    else:
        return None
    if tags.get("foot") == "no":
        return None
    if tags.get("access") in ("no", "private") and tags.get("foot") not in FUSS_JA:
        return None
    return art


# Zufahrt mit dem Auto (Meilenstein 19a): Straßen, auf denen private Autos fahren dürfen
AUTO_STRASSEN = {"motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link", "secondary",
                 "secondary_link", "tertiary", "tertiary_link", "unclassified", "residential", "living_street",
                 "service", "road"}
# Gesperrt für private Autos; „destination“ (nur Anlieger) zählt als gesperrt (Entscheidung des Nutzers)
AUTO_GESPERRT = {"no", "private", "agricultural", "forestry", "destination", "delivery", "permit", "emergency",
                 "military", "customers"}
AUTO_PUFFER = 0.3  # Grad: das Straßennetz reicht so weit über das Gebiet hinaus
ZUFAHRT_NAH = 80.0  # Meter: Autostraßen so nah am Parkplatz zählen als seine Zufahrt
ZUFAHRT_FERN = 300.0  # Meter: keine freie Autostraße so nah → keine Zufahrt
# Bits im Startpunkt (Byte 17)
KEINE_ZUFAHRT, MAUT, ZEITWEISE = 1, 2, 4


def autofrei(tags) -> bool | None:
    """True: private Autos dürfen fahren; False: gesperrt; None: keine Straße. Forstwege (`track`)
    sind ohne ausdrückliche Freigabe gesperrt (in Bayern und Tirol die Regel). Die genaueste Angabe
    gilt: motorcar vor motor_vehicle vor vehicle vor access."""
    h = tags.get("highway")
    if h not in AUTO_STRASSEN and h != "track":
        return None
    if h == "service" and tags.get("service") == "parking_aisle":
        return None
    for schluessel in ("motorcar", "motor_vehicle", "vehicle", "access"):
        wert = tags.get(schluessel)
        if wert is not None:
            return wert.split(";")[0].strip() not in AUTO_GESPERRT
    return h != "track"


def maut(tags) -> bool:
    return tags.get("toll") == "yes" or tags.get("toll:motorcar") == "yes"


def zeitweise_gesperrt(tags) -> bool:
    """Zeitweise Sperre (z. B. „no @ (Nov–Apr)“); ausgewertet wird nur, dass es eine gibt."""
    return any(wert.split("@")[0].strip() in AUTO_GESPERRT
               for schluessel in ("motorcar:conditional", "motor_vehicle:conditional", "vehicle:conditional",
                                  "access:conditional")
               if (wert := tags.get(schluessel)))


def startart(tags) -> int | None:
    if tags.get("amenity") == "parking":
        if tags.get("access") in ("private", "no", "customers") or tags.get("parking") == "underground":
            return None
        return PARKPLATZ
    if tags.get("railway") in ("station", "halt"):
        return BAHNHOF
    if tags.get("highway") == "bus_stop" or tags.get("public_transport") == "platform":
        return HALTESTELLE
    if tags.get("tourism") in ("alpine_hut", "wilderness_hut"):
        return HUETTE
    if tags.get("aerialway") == "station":
        return SEILBAHN
    return None


STANDARDNAME = {PARKPLATZ: "Parkplatz", HALTESTELLE: "Haltestelle", BAHNHOF: "Bahnhof", HUETTE: "Hütte",
                SEILBAHN: "Bergbahnstation"}


# Orte mit Aussicht (Byte, muss zu core `Ortsart` passen; Meilenstein 18b)
GIPFEL, SATTEL, AUSSICHT, ORT_HUETTE = range(4)
ORTSNAME = {GIPFEL: "Gipfel", SATTEL: "Sattel", AUSSICHT: "Aussichtspunkt", ORT_HUETTE: "Hütte"}
HOEHE_UNBEKANNT = -2**31


def ortsart(tags) -> int | None:
    """Gipfel und Sättel nur mit Namen (sonst zu viele), Aussichtspunkte und Hütten immer."""
    name = tags.get("name")
    if tags.get("natural") in ("peak", "volcano") and name:
        return GIPFEL
    if (tags.get("natural") == "saddle" or tags.get("mountain_pass") == "yes") and name:
        return SATTEL
    if tags.get("tourism") == "viewpoint":
        return AUSSICHT
    if tags.get("tourism") in ("alpine_hut", "wilderness_hut"):
        return ORT_HUETTE
    return None


def ele(tags) -> float | None:
    """OSM-Höhe (`ele`, z. B. „1884“, „1884 m“, „1884,5“) in Metern, sonst None."""
    roh = (tags.get("ele") or "").replace(",", ".").strip()
    zahl = ""
    for z in roh:
        if z.isdigit() or z in ".-":
            zahl += z
        else:
            break
    try:
        return float(zahl)
    except ValueError:
        return None


def ist_bahnhalt(tags) -> bool:
    """Halt einer Bahn (Bahnhof, Haltepunkt, Bahnsteig) – nicht Bushaltestelle."""
    if tags.get("highway") == "bus_stop" or tags.get("bus") == "yes":
        return False
    return tags.get("railway") in ("station", "halt", "platform") or tags.get("public_transport") == "platform"


def ist_bergbahn(tags) -> bool:
    """Gleis einer Zahnrad- oder Standseilbahn: Wer dort einsteigt, fährt Bahn statt zu gehen.
    Halte daran zählen wie Seilbahnstationen und sind nie Startpunkt der Wegsuche (Seilbahnzeiten
    zählen nicht, nur Gehzeiten)."""
    if tags.get("railway") == "funicular":
        return True
    zahnrad = tags.get("rack") or tags.get("railway:rack")
    return tags.get("railway") in ("rail", "narrow_gauge", "light_rail") and zahnrad not in (None, "no")


class Sammler(osmium.SimpleHandler):
    def __init__(self, gebiet: hm.Gebiet):
        super().__init__()
        self.g = gebiet
        self.wege: dict[int, Weg] = {}
        # Startpunkte: Breite, Länge, Art, Name, Bahnhalt (für die Prüfung auf Bergbahnen)
        self.starts: dict[tuple[str, int], tuple[float, float, int, str, bool]] = {}
        self.bergbahn: list[np.ndarray] = []  # Punkte (Breite, Länge) von Zahnrad- und Standseilbahnen
        # Autostraßen: Knoten-IDs, Breite, Länge, frei, Maut, zeitweise gesperrt
        self.auto: list[tuple[np.ndarray, np.ndarray, np.ndarray, bool, bool, bool]] = []
        # Orte mit Aussicht: Breite, Länge, Art, Name, OSM-Höhe (oder None)
        self.orte: dict[tuple[str, int], tuple[float, float, int, str, float | None]] = {}

    def innen(self, b: float, l: float) -> bool:
        return self.g.sued <= b <= self.g.nord and self.g.west <= l <= self.g.ost

    def node(self, n):
        ort = ortsart(n.tags)
        if ort is not None and n.location.valid() and self.innen(n.location.lat, n.location.lon):
            self.orte[("n", n.id)] = (n.location.lat, n.location.lon, ort, n.tags.get("name", ""), ele(n.tags))
        art = startart(n.tags)
        if art is None or not n.location.valid() or not self.innen(n.location.lat, n.location.lon):
            return
        self.starts[("n", n.id)] = (n.location.lat, n.location.lon, art, n.tags.get("name", ""), ist_bahnhalt(n.tags))

    def auto_innen(self, b: np.ndarray, l: np.ndarray) -> bool:
        return bool(((b >= self.g.sued - AUTO_PUFFER) & (b <= self.g.nord + AUTO_PUFFER) &
                     (l >= self.g.west - AUTO_PUFFER) & (l <= self.g.ost + AUTO_PUFFER)).any())

    def way(self, w):
        frei = autofrei(w.tags)
        if frei is not None:
            try:
                ids = np.fromiter((n.ref for n in w.nodes), dtype=np.int64)
                b = np.fromiter((n.lat for n in w.nodes), dtype=np.float64)
                l = np.fromiter((n.lon for n in w.nodes), dtype=np.float64)
            except osmium.InvalidLocationError:
                ids = np.zeros(0, dtype=np.int64)
            if len(ids) >= 2 and self.auto_innen(b, l):
                self.auto.append((ids, b, l, frei, maut(w.tags), zeitweise_gesperrt(w.tags)))
        if ist_bergbahn(w.tags):
            try:
                self.bergbahn.append(np.array([(n.lat, n.lon) for n in w.nodes], dtype=np.float64))
            except osmium.InvalidLocationError:
                pass
            return
        art = begehbar(w.tags)
        ort = ortsart(w.tags) if art is None else None
        if ort is not None:  # Hütte oder Aussichtspunkt als Fläche: Mittelpunkt
            try:
                punkte = [(n.lat, n.lon) for n in w.nodes]
            except osmium.InvalidLocationError:
                punkte = []
            if punkte:
                b0 = sum(p[0] for p in punkte) / len(punkte)
                l0 = sum(p[1] for p in punkte) / len(punkte)
                if self.innen(b0, l0):
                    self.orte[("w", w.id)] = (b0, l0, ort, w.tags.get("name", ""), ele(w.tags))
        start = startart(w.tags) if art is None else None
        if art is None and start is None:
            return
        try:
            ids = np.fromiter((n.ref for n in w.nodes), dtype=np.int64)
            b = np.fromiter((n.lat for n in w.nodes), dtype=np.float64)
            l = np.fromiter((n.lon for n in w.nodes), dtype=np.float64)
        except osmium.InvalidLocationError:
            return
        if len(ids) < 2 and art is not None:
            return
        drin = (b >= self.g.sued) & (b <= self.g.nord) & (l >= self.g.west) & (l <= self.g.ost)
        if not drin.any():
            return
        if start is not None:  # Parkplatz als Fläche: Mittelpunkt
            self.starts[("w", w.id)] = (float(b.mean()), float(l.mean()), start, w.tags.get("name", ""),
                                        ist_bahnhalt(w.tags))
            return
        if w.id in self.wege:  # aus dem Nachbar-Extrakt schon da
            return
        self.wege[w.id] = Weg(ids, b, l, art, SAC.get(w.tags.get("sac_scale"), 0),
                              SICHT.get(w.tags.get("trail_visibility"), 0),
                              w.tags.get("name") or w.tags.get("ref") or "")


class Hoehen:
    """Wie `Hoehenmodell` in core: feinste Ebene, die den Punkt mit allen vier Nachbarn abdeckt, bilinear."""

    def __init__(self, pfade: Path | list[Path]):
        # Mehrere Dateien (Höhenkacheln der Regionen): alle Ebenen, feinste zuerst
        pfade = [pfade] if isinstance(pfade, Path) else pfade
        self.ebenen = sorted((e for p in pfade for e in hm.lies(p)), key=lambda e: e.schritt_bogensek)

    def abtasten(self, b: np.ndarray, l: np.ndarray) -> np.ndarray:
        ergebnis = np.full(b.shape, np.nan)
        offen = np.ones(b.shape, dtype=bool)
        for e in self.ebenen:
            if not offen.any():
                break
            zeilen, spalten = e.hoehen.shape
            z = (e.nord - b[offen] * 3600 * e.teilung) / e.schritt
            s = (l[offen] * 3600 * e.teilung - e.west) / e.schritt
            innen = (z >= 0) & (z <= zeilen - 1) & (s >= 0) & (s <= spalten - 1)
            if not innen.any():
                continue
            zi, si = z[innen], s[innen]
            z0, z1 = int(np.floor(zi.min())), int(min(zeilen, np.ceil(zi.max()) + 2))
            s0, s1 = int(np.floor(si.min())), int(min(spalten, np.ceil(si.max()) + 2))
            werte = map_coordinates(e.meter(slice(z0, z1), slice(s0, s1)), [zi - z0, si - s0], order=1,
                                    mode="nearest", cval=np.nan)
            idx = np.flatnonzero(offen)[np.flatnonzero(innen)]
            gut = np.isfinite(werte)
            ergebnis[idx[gut]] = werte[gut]
            offen[idx[gut]] = False
        return ergebnis


def halb(x: float) -> int:
    """Kaufmännisch runden wie Kotlins Math.round (Pythons round rundet .5 zur geraden Zahl)."""
    return math.floor(x + 0.5)


def meter(b1, l1, b2, l2) -> np.ndarray:
    """Haversine in Metern (Arrays in Grad)."""
    p1, p2 = np.radians(b1), np.radians(b2)
    h = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(np.radians(l2 - l1) / 2) ** 2
    return 2 * ERDRADIUS * np.arcsin(np.sqrt(h))


def zufahrten(sammler: Sammler, cos0: float) -> dict[tuple[str, int], int]:
    """Zufahrt je Parkplatz (Meilenstein 19a, tools/wege/README.md): Ist er über Straßen, auf denen
    private Autos fahren dürfen, mit dem großen öffentlichen Netz verbunden? Dazu Maut und zeitweise
    Sperren auf allen solchen Zufahrten. Ergebnis: Bits KEINE_ZUFAHRT, MAUT, ZEITWEISE."""
    freie = [s for s in sammler.auto if s[3]]
    if not freie:
        return {}
    ids = np.concatenate([s[0] for s in freie])
    b = np.concatenate([s[1] for s in freie])
    l = np.concatenate([s[2] for s in freie])
    knoten, erste, idx = np.unique(ids, return_index=True, return_inverse=True)
    kb, kl = b[erste], l[erste]

    def netz(ohne_maut: bool, ohne_zeitweise: bool) -> np.ndarray:
        """Kennzeichnet je Knoten, ob er zum größten Netzteil gehört."""
        von, nach = [], []
        start = 0
        for s in freie:
            n = len(s[0])
            if not ((ohne_maut and s[4]) or (ohne_zeitweise and s[5])):
                von.append(idx[start:start + n - 1])
                nach.append(idx[start + 1:start + n])
            start += n
        a = np.concatenate(von) if von else np.zeros(0, dtype=np.int64)
        z = np.concatenate(nach) if nach else np.zeros(0, dtype=np.int64)
        graph = coo_matrix((np.ones(len(a), dtype=np.int8), (a, z)), shape=(len(knoten), len(knoten)))
        _, teil = connected_components(graph, directed=False)
        groesster = np.bincount(teil).argmax()
        return teil == groesster

    im_netz, ohne_maut, ohne_zeit = netz(False, False), netz(True, False), netz(False, True)
    baum = cKDTree(np.column_stack([kb * 111_195.0, kl * 111_195.0 * cos0]))
    ergebnis: dict[tuple[str, int], int] = {}
    gesperrt = maut_n = zeit_n = 0
    for k, (pb, pl, art, _, _) in sammler.starts.items():
        if art != PARKPLATZ:
            continue
        punkt = [pb * 111_195.0, pl * 111_195.0 * cos0]
        nah = baum.query_ball_point(punkt, ZUFAHRT_NAH)
        if not nah:
            d, i = baum.query(punkt)
            nah = [i] if d <= ZUFAHRT_FERN else []
        if not any(im_netz[i] for i in nah):
            ergebnis[k] = KEINE_ZUFAHRT
            gesperrt += 1
            continue
        bits = 0
        if not any(ohne_maut[i] for i in nah):
            bits |= MAUT
            maut_n += 1
        if not any(ohne_zeit[i] for i in nah):
            bits |= ZEITWEISE
            zeit_n += 1
        ergebnis[k] = bits
    print(f"  Zufahrt: {len(knoten)} Knoten im Autonetz; Parkplätze ohne Zufahrt {gesperrt}, "
          f"nur über Maut {maut_n}, zeitweise gesperrt {zeit_n}")
    return ergebnis


def verdichten(punkte: np.ndarray, schritt: float) -> np.ndarray:
    """Linie (Breite, Länge in Grad) mit Zwischenpunkten alle [schritt] Meter, damit der Abstand zu den
    Punkten auch mitten auf langen geraden Stücken stimmt."""
    if len(punkte) < 2:
        return punkte
    teile = [punkte[:1]]
    for a, b in zip(punkte[:-1], punkte[1:]):
        n = max(1, math.ceil(float(meter(a[0], a[1], b[0], b[1])) / schritt))
        u = np.linspace(0, 1, n + 1)[1:, None]
        teile.append(a + u * (b - a))
    return np.concatenate(teile)


def douglas_peucker(b: np.ndarray, l: np.ndarray, toleranz: float) -> np.ndarray:
    """Indizes der zu behaltenden Punkte (eben genähert, für kurze Wegstücke ausreichend)."""
    if len(b) <= 2:
        return np.arange(len(b))
    y = np.radians(b) * ERDRADIUS
    x = np.radians(l) * ERDRADIUS * math.cos(math.radians(float(b[0])))
    behalten = np.zeros(len(b), dtype=bool)
    behalten[[0, -1]] = True
    stapel = [(0, len(b) - 1)]
    while stapel:
        a, e = stapel.pop()
        if e <= a + 1:
            continue
        dx, dy = x[e] - x[a], y[e] - y[a]
        n = math.hypot(dx, dy)
        px, py = x[a + 1:e] - x[a], y[a + 1:e] - y[a]
        d = np.abs(px * dy - py * dx) / n if n > 0 else np.hypot(px, py)
        i = int(np.argmax(d))
        if d[i] > toleranz:
            k = a + 1 + i
            behalten[k] = True
            stapel += [(a, k), (k, e)]
    return np.flatnonzero(behalten)


def auf_ab(b: np.ndarray, l: np.ndarray, hoehen: Hoehen) -> tuple[float, float, float, float]:
    """Länge, Aufstieg, Abstieg (Meter, in Richtung der Punkte) und Höhe am Ende des Stücks."""
    d = meter(b[:-1], l[:-1], b[1:], l[1:])
    laenge = float(d.sum())
    # Punkte alle HOEHENSCHRITT Meter entlang der Linie
    s = np.concatenate([[0.0], np.cumsum(d)])
    n = max(2, int(math.ceil(laenge / HOEHENSCHRITT)) + 1)
    t = np.linspace(0, laenge, n)
    z = hoehen.abtasten(np.interp(t, s, b), np.interp(t, s, l))
    z = np.where(np.isfinite(z), z, np.nan)
    if np.isnan(z).all():
        return laenge, 0.0, 0.0, float("nan")
    z = np.interp(t, t[np.isfinite(z)], z[np.isfinite(z)])
    dz = np.diff(z)
    return laenge, float(dz[dz > 0].sum()), float(-dz[dz < 0].sum()), float(z[-1])


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pbf", type=Path, nargs="+", required=True)
    p.add_argument("--hoehen", type=Path, nargs="+", required=True)
    p.add_argument("--ausgabe", type=Path, required=True)
    p.add_argument("--gebiet", help="süd,nord,west,ost in Grad (Standard: Kerngebiet)")
    p.add_argument("--stand", default=None, help="Datenstand JJJJ-MM-TT (Standard: Datum der ersten PBF)")
    a = p.parse_args()
    gebiet = hm.KERN if not a.gebiet else hm.Gebiet(*map(float, a.gebiet.split(",")))
    beginn = time.time()

    sammler = Sammler(gebiet)
    for quelle in a.pbf:
        print(f"Lese {quelle.name} …", flush=True)
        sammler.apply_file(str(quelle), locations=True, idx="flex_mem")
    wege = list(sammler.wege.values())
    print(f"  {len(wege)} Wege, {len(sammler.starts)} mögliche Startpunkte ({time.time() - beginn:.0f} s)")

    # Startpunkte an den nächsten Wegpunkt fangen; der wird dann Graphknoten
    alle_ids = np.concatenate([w.knoten for w in wege])
    alle_b = np.concatenate([w.breite for w in wege])
    alle_l = np.concatenate([w.laenge for w in wege])
    cos0 = math.cos(math.radians((gebiet.sued + gebiet.nord) / 2))
    baum = cKDTree(np.column_stack([alle_b * 111_195.0, alle_l * 111_195.0 * cos0]))
    # Halte von Zahnrad- und Standseilbahnen (Wendelstein, Zugspitze, Rosshütte …) wie Seilbahnstationen
    if sammler.bergbahn:
        gleis = np.concatenate([verdichten(g, 10.0) for g in sammler.bergbahn])
        gleisbaum = cKDTree(np.column_stack([gleis[:, 0] * 111_195.0, gleis[:, 1] * 111_195.0 * cos0]))
        bergbahnhalte = 0
        for k, (b, l, art, name, bahn) in list(sammler.starts.items()):
            if bahn and art in (BAHNHOF, HALTESTELLE):
                d, _ = gleisbaum.query([b * 111_195.0, l * 111_195.0 * cos0])
                if d <= BERGBAHN_NAH:
                    sammler.starts[k] = (b, l, SEILBAHN, name, bahn)
                    bergbahnhalte += 1
        print(f"  {bergbahnhalte} Halte an Zahnrad- und Standseilbahnen")

    zufahrt = zufahrten(sammler, cos0)

    starts = []
    for k, (b, l, art, name, _) in sammler.starts.items():
        d, i = baum.query([b * 111_195.0, l * 111_195.0 * cos0])
        if d <= FANG:
            starts.append((b, l, art, name or STANDARDNAME[art], int(alle_ids[i]), zufahrt.get(k, 0)))
    print(f"  {len(starts)} Startpunkte am Wegenetz")

    # Graphknoten: Wegenden, Kreuzungen (Knoten mehrfach benutzt), Startpunkte
    nutzung = Counter(alle_ids.tolist())
    for w in wege:
        nutzung[int(w.knoten[0])] += 1
        nutzung[int(w.knoten[-1])] += 1
    for s in starts:
        nutzung[s[4]] += 2
    graph_ids = sorted(k for k, n in nutzung.items() if n >= 2)
    index = {k: i for i, k in enumerate(graph_ids)}
    del nutzung
    lage: dict[int, tuple[float, float]] = {}
    for w in wege:
        for k, b, l in zip(w.knoten.tolist(), w.breite.tolist(), w.laenge.tolist()):
            if k in index:
                lage[k] = (b, l)
    print(f"  {len(graph_ids)} Graphknoten ({time.time() - beginn:.0f} s)")

    hoehen = Hoehen(a.hoehen)
    kb = np.array([lage[k][0] for k in graph_ids])
    kl = np.array([lage[k][1] for k in graph_ids])
    kz = hoehen.abtasten(kb, kl)
    kz = np.where(np.isfinite(kz), kz, 0.0)

    # Kanten: Wegstücke zwischen aufeinanderfolgenden Graphknoten
    namen: dict[str, int] = {}
    namen_bytes = bytearray()

    def name_index(text: str) -> int:
        if not text:
            return -1
        if text not in namen:
            roh = text.encode("utf-8")[:65535]
            namen[text] = len(namen_bytes)
            namen_bytes.extend(struct.pack("<H", len(roh)) + roh)
        return namen[text]

    kanten = bytearray()
    geometrie = bytearray()
    geo_anzahl = 0
    m = 0
    for nr, w in enumerate(wege):
        ist_knoten = np.array([int(k) in index for k in w.knoten.tolist()])
        stellen = np.flatnonzero(ist_knoten)
        for s0, s1 in zip(stellen[:-1], stellen[1:]):
            b, l = w.breite[s0:s1 + 1], w.laenge[s0:s1 + 1]
            laenge, auf, ab, _ = auf_ab(b, l, hoehen)
            if laenge <= 0.01:
                continue
            behalten = douglas_peucker(b, l, VEREINFACHUNG)[1:-1]
            start = geo_anzahl
            # Start: der gespeicherte Knoten (Mikrograd) auf 1/100 000 Grad gerundet, wie der Leser in core
            vorher = (halb(halb(float(b[0]) * MIKRO) / 10), halb(halb(float(l[0]) * MIKRO) / 10))
            for i in behalten:
                q = (halb(float(b[i]) * GEO), halb(float(l[i]) * GEO))
                db, dl = q[0] - vorher[0], q[1] - vorher[1]
                if abs(db) > 32767 or abs(dl) > 32767:
                    raise SystemExit(f"Geometrieschritt zu groß in Weg {nr}")
                geometrie += struct.pack("<hh", db, dl)
                geo_anzahl += 1
                vorher = q
            kanten += struct.pack(
                "<iifHHBBBBii",
                index[int(w.knoten[s0])], index[int(w.knoten[s1])], laenge,
                min(65535, round(auf * 10)), min(65535, round(ab * 10)),
                w.art, w.sac, w.sicht, 0, start, name_index(w.name),
            )
            m += 1
        if nr % 50_000 == 0:
            print(f"  Wege {nr}/{len(wege)}, {m} Kanten ({time.time() - beginn:.0f} s)", flush=True)

    start_bytes = bytearray()
    for b, l, art, name, osm, flaggen in starts:
        start_bytes += struct.pack("<iiiiBB2x", halb(b * MIKRO), halb(l * MIKRO), index[osm], name_index(name), art,
                                   flaggen)

    # Orte mit Aussicht (Meilenstein 18b): Abschnitt nach den Namen, im Kopf ab Byte 36 angekündigt;
    # ältere Leser überspringen ihn
    orte_bytes = bytearray()
    for b, l, art, name, hoehe in sammler.orte.values():
        z = HOEHE_UNBEKANNT if hoehe is None else halb(hoehe * 10)
        orte_bytes += struct.pack("<iiiiB3x", halb(b * MIKRO), halb(l * MIKRO), z, name_index(name or ORTSNAME[art]), art)
    orte_anzahl = len(sammler.orte)
    print(f"  {orte_anzahl} Orte mit Aussicht")

    stand = a.stand or dt.date.fromtimestamp(a.pbf[0].stat().st_mtime).isoformat()
    n = len(graph_ids)
    def ausgerichtet(laenge: int) -> int:
        return laenge + (-laenge % 4)

    orte_ab = KOPF + 12 * n + ausgerichtet(len(kanten)) + ausgerichtet(len(geometrie)) + ausgerichtet(len(start_bytes)) \
        + ausgerichtet(len(namen_bytes))
    kopf = struct.pack("<8siiiiiiiii", MAGIC, VERSION, n, m, geo_anzahl, len(starts), len(namen_bytes),
                       int(stand.replace("-", "")), orte_anzahl, orte_ab)
    kopf += bytes(KOPF - len(kopf))
    knoten = np.zeros(n, dtype=[("b", "<i4"), ("l", "<i4"), ("z", "<i4")])
    knoten["b"] = np.floor(kb * MIKRO + 0.5)
    knoten["l"] = np.floor(kl * MIKRO + 0.5)
    knoten["z"] = np.floor(kz * 10 + 0.5)
    a.ausgabe.parent.mkdir(parents=True, exist_ok=True)
    teil = a.ausgabe.with_name(a.ausgabe.name + ".teil")
    with open(teil, "wb") as f:
        for teilstueck in (kopf, knoten.tobytes(), kanten, geometrie, start_bytes, namen_bytes, orte_bytes):
            f.write(teilstueck)
            f.write(bytes(-len(teilstueck) % 4))  # 4-Byte-Grenzen
    teil.replace(a.ausgabe)
    print(f"{a.ausgabe}: {n} Knoten, {m} Kanten, {geo_anzahl} Geometriepunkte, {len(starts)} Startpunkte, "
          f"{orte_anzahl} Orte, "
          f"{a.ausgabe.stat().st_size / 1e6:.1f} MB, Stand {stand} ({time.time() - beginn:.0f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
