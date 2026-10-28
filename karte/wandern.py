"""Erzeugt die Wander-Ebene der Offline-Karte direkt aus OpenStreetMap.

Liest Geofabrik-Extrakte (.osm.pbf), wählt alles aus, was eine Alpenvereins- bzw. Wanderkarte
zeigt, und schreibt die Vektorkacheln mondschein-wandern.pmtiles (Zoom 9–14, darüber vergrößert).

Ebenen in den Kacheln:
  wege     Wege und Steige mit Schwierigkeit (sac_scale), Sichtbarkeit, Klettersteig-Grad,
           Wegnummern und Fernwanderwegen aus den Routen-Relationen
  bahnen   Seilbahnen und Lifte
  huetten  Hütten, Biwaks, Unterstände, Einkehr-Almen
  gipfel   Gipfel mit Höhe, Gipfelkreuz und Mindestzoom nach Dominanz
  punkte   Sättel, Quellen, Höhlen, Wasserfälle, Trinkwasser, Aussichtspunkte, Wegkreuze,
           Kapellen, Parkplätze, Haltestellen, Seilbahnstationen

Aufruf:
    python wandern.py --pbf <a.osm.pbf> [<b.osm.pbf> …] --hoehen <mondschein-hoehen.bin> --ausgabe <ordner>
"""

from __future__ import annotations

import argparse
import gzip
import math
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import mapbox_vector_tile
import numpy as np
import osmium
import shapely
from pmtiles.tile import Compression, TileType, zxy_to_tileid
from shapely.geometry import LineString, Point

import gelaende
from gelaende import Hoehen, kacheln_im_gebiet, schreibe

ZOOM = range(9, 15)
MAX_ZOOM = ZOOM.stop - 1
EXTENT = 4096
NORD_GEBIRGE = 47.90  # Nordrand des Kerngebiets: Parkplätze/Haltestellen nur südlich davon
RAND = 64  # Kachelpuffer in Kachelkoordinaten, damit Linien und Symbole an Kanten nicht abreißen

# ---------------------------------------------------------------- Auswahl

WEG_ARTEN = {"path", "footway", "track", "steps", "bridleway", "via_ferrata"}
SAC = {
    "hiking": 1, "mountain_hiking": 2, "demanding_mountain_hiking": 3,
    "alpine_hiking": 4, "demanding_alpine_hiking": 5, "difficult_alpine_hiking": 6,
}
SICHT = {"excellent": 1, "good": 2, "intermediate": 3, "bad": 4, "horrible": 5, "no": 6}
NETZ = {"iwn": 4, "nwn": 3, "rwn": 2, "lwn": 1}  # ab 3 = Fernwanderweg (AT: 01–10, E-Wege, Via Alpina)
FERN = 3
# Klettersteig: via_ferrata_scale 0–6 ≈ Schall-Skala A–F (Werte in OSM teils „2+“, „1.5“ …)
KS_GRAD = {0: "A", 1: "A", 2: "B", 3: "C", 4: "D", 5: "E", 6: "F"}
FILTER_VERSION = 2  # erhöhen, wenn sich die Auswahl in poi_klasse/relevanter_weg ändert
BAHN_ARTEN = {"cable_car", "gondola", "mixed_lift", "chair_lift", "drag_lift", "t-bar", "j-bar",
              "platter", "rope_tow", "zip_line", "goods"}
VEREINE = re.compile(r"\b(DAV|ÖAV|OeAV|OAV|AVS|SAC|Alpenverein|Naturfreunde|TVN|ÖTK|Touristenklub|"
                     r"Sektion|Alpenvereins)\b", re.IGNORECASE)
HUETTENNAME = re.compile(r"(hütte|huette|hütt'n|haus|alm\b|alm |almhütte|alpe|kaser|stüberl|"
                         r"diensthütte|biwak)", re.IGNORECASE)


def ist_ja(wert: str | None) -> bool:
    return wert is not None and wert.lower() in ("yes", "true", "1")


def poi_klasse(tags) -> str | None:
    """Kategorie eines Punkts (oder Gebäudes, das als Punkt gezeichnet wird)."""
    t = tags.get
    tourism, amenity, natural = t("tourism"), t("amenity"), t("natural")
    if tourism == "alpine_hut":
        return "huette"
    if tourism == "wilderness_hut":
        return "biwak" if "biwak" in (t("name") or "").lower() else "selbstversorger"
    if amenity == "shelter":
        art = t("shelter_type")
        if art in ("basic_hut", "rock_shelter") or "biwak" in (t("name") or "").lower():
            return "biwak"
        if art in ("weather_shelter", "lean_to", None):
            return "unterstand"
        return None
    if amenity in ("restaurant", "cafe", "biergarten", "pub"):
        return "einkehr?"  # wird über Name und Geländehöhe entschieden
    if natural == "peak" and t("name") or natural == "peak" and t("ele"):
        return "gipfel"
    if natural == "saddle" or t("mountain_pass") == "yes":
        return "sattel"
    if natural == "spring":
        return "quelle"
    if natural == "cave_entrance":
        return "hoehle"
    if t("waterway") == "waterfall":
        return "wasserfall"
    if amenity == "drinking_water" or t("man_made") == "water_tap" and ist_ja(t("drinking_water")):
        return "trinkwasser"
    if amenity == "fountain" and ist_ja(t("drinking_water")):
        return "trinkwasser"
    if tourism == "viewpoint":
        return "aussicht"
    if t("man_made") == "cross" and (ist_ja(t("summit:cross")) or t("summit:cross") == "cross"):
        return "gipfelkreuz"  # wird dem nächsten Gipfel zugeordnet
    if t("historic") == "wayside_cross" or t("man_made") == "cross":
        return "kreuz"
    if t("historic") == "wayside_shrine":
        return "marterl"
    if t("historic") == "wayside_chapel" or amenity == "place_of_worship" and (
            t("building") == "chapel" or "kapelle" in (t("name") or "").lower()):
        return "kapelle"
    if amenity == "parking" and t("access") not in ("private", "no", "customers"):
        return "parkplatz"
    if t("highway") == "bus_stop" or t("public_transport") == "platform" and t("bus") == "yes":
        return "haltestelle"
    if t("aerialway") == "station":
        return "station"
    return None


def relevanter_weg(tags) -> bool:
    return (tags.get("highway") in WEG_ARTEN or "sac_scale" in tags or tags.get("aerialway") in BAHN_ARTEN
            or poi_klasse(tags) is not None)


# ---------------------------------------------------------------- Lesen

def filtern(quelle: Path, ziel: Path) -> None:
    """Schreibt nur relevante Objekte samt aller referenzierten Knoten/Wege in eine kleine Datei."""
    if ziel.exists() and ziel.stat().st_mtime > quelle.stat().st_mtime:
        print(f"  {ziel.name} ist aktuell")
        return
    start = time.time()
    with osmium.BackReferenceWriter(str(ziel), ref_src=str(quelle), overwrite=True) as w:
        for o in osmium.FileProcessor(str(quelle)).with_filter(osmium.filter.EmptyTagFilter()):
            if o.is_node():
                if poi_klasse(o.tags):
                    w.add(o)
            elif o.is_way():
                if relevanter_weg(o.tags):
                    w.add(o)
            elif o.tags.get("type") == "route" and o.tags.get("route") in ("hiking", "foot"):
                w.add(o)
    print(f"  {quelle.name} → {ziel.name}: {ziel.stat().st_size / 1e6:.0f} MB in {time.time() - start:.0f} s")


class Sammler:
    def __init__(self, hoehen: Hoehen):
        self.hoehen = hoehen
        self.routen: dict[int, list[dict]] = defaultdict(list)  # Weg-ID → Routen
        self.wege: dict[int, dict] = {}
        self.bahnen: dict[int, dict] = {}
        self.punkte: dict[tuple[str, int], dict] = {}

    def routen_lesen(self, datei: Path) -> None:
        for r in osmium.FileProcessor(str(datei), osmium.osm.RELATION):
            t = r.tags
            if t.get("type") != "route" or t.get("route") not in ("hiking", "foot"):
                continue
            route = {"ref": t.get("ref"), "name": t.get("name"), "netz": NETZ.get(t.get("network"), 1),
                     "id": r.id}
            for m in r.members:
                if m.type == "w":
                    if all(x["id"] != r.id for x in self.routen[m.ref]):
                        self.routen[m.ref].append(route)

    def objekte_lesen(self, datei: Path) -> None:
        for o in osmium.FileProcessor(str(datei), osmium.osm.NODE | osmium.osm.WAY).with_locations():
            if o.is_node():
                if o.tags and (k := poi_klasse(o.tags)):
                    self._punkt(("n", o.id), k, o.tags, o.location.lon, o.location.lat)
                continue
            koord = [(n.lon, n.lat) for n in o.nodes if n.location.valid()]
            if len(koord) < 2 or not im_gebiet(koord):
                continue
            t = o.tags
            if t.get("aerialway") in BAHN_ARTEN:
                self.bahnen[o.id] = {"koord": koord, "art": t.get("aerialway"), "name": t.get("name")}
            elif t.get("highway") in WEG_ARTEN or "sac_scale" in t:
                self._weg(o.id, t, koord)
            elif (k := poi_klasse(t)) and koord[0] == koord[-1]:
                # Gebäude oder Fläche als Punkt im Schwerpunkt
                lon, lat = np.mean(koord[:-1], axis=0)
                self._punkt(("w", o.id), k, t, float(lon), float(lat))

    def _weg(self, wid: int, t, koord) -> None:
        art = t.get("highway") or "path"
        routen = self.routen.get(wid, [])
        if art == "footway" and "sac_scale" not in t and not routen:
            return  # Gehsteige und Fußwege im Ort: zeichnet die Grundkarte
        refs = sorted({r["ref"] for r in routen if r["ref"]}, key=lambda s: (len(s), s))
        fern = max((r for r in routen if r["netz"] >= FERN), key=lambda r: r["netz"], default=None)
        self.wege[wid] = {
            "koord": koord,
            "art": art,
            "sac": SAC.get(t.get("sac_scale"), 0),
            "sicht": SICHT.get(t.get("trail_visibility"), 0),
            "ks": klettersteig_grad(t.get("via_ferrata_scale")) if art == "via_ferrata" else "",
            "ref": " · ".join(refs[:3]),
            "netz": max((r["netz"] for r in routen), default=0),
            "fernweg": (fern["name"] or fern["ref"] or "") if fern else "",
            "name": t.get("name") or "",
            "tunnel": int(t.get("tunnel") not in (None, "no")),
            "bruecke": int(t.get("bridge") not in (None, "no")),
        }

    def _punkt(self, schluessel, klasse: str, t, lon: float, lat: float) -> None:
        if not (gelaende.WEST <= lon <= gelaende.OST and gelaende.SUED <= lat <= gelaende.NORD):
            return
        gelaende = None
        if klasse in ("einkehr?", "parkplatz"):
            gelaende = float(self.hoehen.abtasten(np.array([lat]), np.array([lon]))[0])
        if klasse == "einkehr?":
            # Almausschank/Berggasthof: Hüttenname ab 800 m oder benannte Gastronomie ab 1100 m;
            # ohne Namen sind es meist Kioske und Bars an Bergstationen und im Skigebiet.
            if not t.get("name") or not (gelaende >= 1100 or gelaende >= 800 and HUETTENNAME.search(t.get("name"))):
                return
            klasse = "einkehr"
        if klasse == "parkplatz" and not (ist_ja(t.get("hiking")) or lat <= NORD_GEBIRGE and gelaende >= 700):
            return  # nur Wanderparkplätze, nicht jeder Parkplatz im Ort
        if klasse == "haltestelle" and lat > NORD_GEBIRGE:
            return  # im Flachland (München) nur Ballast
        if klasse == "huette" and t.get("amenity") == "restaurant" and not VEREINE.search(
                " ".join(filter(None, (t.get("operator"), t.get("name"))))):
            klasse = "berggasthof"  # alpine_hut + Restaurant ohne Verein: oft Berggasthaus/Alm
        p = {"lon": lon, "lat": lat, "klasse": klasse, "name": t.get("name") or ""}
        if (ele := hoehe_aus_tag(t.get("ele"))) is not None:
            p["hoehe"] = ele
        if klasse == "gipfel":
            p["kreuz"] = int(ist_ja(t.get("summit:cross")) or t.get("summit:cross") == "cross")
        if klasse in ("huette", "berggasthof", "selbstversorger", "biwak"):
            p["verein"] = int(bool(VEREINE.search(" ".join(filter(None, (t.get("operator"), t.get("name")))))))
            p["winterraum"] = int(ist_ja(t.get("winter_room")))
            if (betten := t.get("beds") or t.get("capacity")) and betten.isdigit():
                p["betten"] = int(betten)
        if klasse == "station":
            p["name"] = t.get("name") or ""
        if klasse == "parkplatz" and ist_ja(t.get("hiking")):
            p["wander"] = 1  # ausgewiesener Wanderparkplatz
        self.punkte[schluessel] = p


def klettersteig_grad(wert: str | None) -> str:
    """„2+“, „1.5“, „3-“ … → Buchstabe A–F; unbekannt → leer."""
    m = re.match(r"^\s*(\d+(?:[.,]\d+)?)", wert or "")
    if not m:
        return ""
    return KS_GRAD[min(6, max(0, round(float(m.group(1).replace(",", ".")))))]


def hoehe_aus_tag(wert: str | None) -> int | None:
    if not wert:
        return None
    m = re.match(r"^\s*(\d{2,4}(?:[.,]\d+)?)\s*(m)?\s*$", wert)
    return round(float(m.group(1).replace(",", "."))) if m else None


def im_gebiet(koord) -> bool:
    lons = [c[0] for c in koord]
    lats = [c[1] for c in koord]
    return max(lons) >= gelaende.WEST and min(lons) <= gelaende.OST and max(lats) >= gelaende.SUED and min(lats) <= gelaende.NORD


# ---------------------------------------------------------------- Bedeutung → Mindestzoom

def gipfel_dominanz(punkte: list[dict]) -> None:
    """Mindestzoom nach Dominanz: Entfernung zum nächsten höheren Gipfel (wie Generalisierung
    auf amtlichen Karten). Wer weit und breit der höchste ist, erscheint früh."""
    gipfel = [p for p in punkte if p["klasse"] == "gipfel" and "hoehe" in p]
    gipfel.sort(key=lambda p: -p["hoehe"])
    if not gipfel:
        return
    y = np.array([p["lat"] for p in gipfel]) * 111_200
    x = np.array([p["lon"] for p in gipfel]) * 111_200 * math.cos(math.radians(47.7))
    for i, p in enumerate(gipfel):
        if i == 0:
            dominanz = 1e9
        else:
            dominanz = float(np.min(np.hypot(x[:i] - x[i], y[:i] - y[i])))
        p["dominanz"] = round(dominanz)
        p["minzoom"] = 9 if dominanz > 15_000 else 10 if dominanz > 6_000 else 11 if dominanz > 2_500 else \
            12 if dominanz > 1_000 else 13 if p["name"] else 14
    for p in punkte:
        if p["klasse"] == "gipfel" and "minzoom" not in p:
            p["minzoom"] = 14


def gipfelkreuze_zuordnen(punkte: dict) -> None:
    """Gipfelkreuze, die als eigener Punkt erfasst sind, markieren den nächsten Gipfel (≤ 80 m)."""
    gipfel = [p for p in punkte.values() if p["klasse"] == "gipfel"]
    if not gipfel:
        return
    gy = np.array([p["lat"] for p in gipfel]) * 111_200
    gx = np.array([p["lon"] for p in gipfel]) * 111_200 * math.cos(math.radians(47.7))
    for schluessel, k in list(punkte.items()):
        if k["klasse"] != "gipfelkreuz":
            continue
        d = np.hypot(gx - k["lon"] * 111_200 * math.cos(math.radians(47.7)), gy - k["lat"] * 111_200)
        i = int(np.argmin(d))
        if d[i] <= 80:
            gipfel[i]["kreuz"] = 1
            del punkte[schluessel]
        else:
            k["klasse"] = "kreuz"


HUETTEN_KLASSEN = ("huette", "berggasthof", "selbstversorger", "biwak", "unterstand", "einkehr")
GIPFELNAH = 150  # m: Hütte direkt am Gipfel (Münchner Haus, Wankhaus, Hochfellnhaus …)
GIPFEL_RANG = {"huette": 0, "berggasthof": 1, "selbstversorger": 2, "biwak": 3, "einkehr": 4}


def huetten_am_gipfel(punkte: dict) -> int:
    """Das wichtigste Haus bis 150 m neben einem Gipfel bekommt „seite“ (+1 östlich, −1 westlich):
    Der Stil rückt sein Symbol zur Seite und zeichnet es immer, sonst verdrängen sich Gipfel und
    Hütte gegenseitig. Weitere Häuser am selben Gipfel (z. B. die Gastronomie der Zugspitz-
    Bergstation) bleiben normale Punkte und weichen dem Gipfel aus."""
    gipfel = [p for p in punkte.values() if p["klasse"] == "gipfel"]
    if not gipfel:
        return 0
    kx = 111_200 * math.cos(math.radians(47.7))
    gx = np.array([p["lon"] for p in gipfel]) * kx
    gy = np.array([p["lat"] for p in gipfel]) * 111_200
    beste: dict[int, tuple] = {}
    for h in punkte.values():
        if h["klasse"] not in GIPFEL_RANG:
            continue
        d = np.hypot(gx - h["lon"] * kx, gy - h["lat"] * 111_200)
        i = int(np.argmin(d))
        rang = (GIPFEL_RANG[h["klasse"]], -h.get("verein", 0), float(d[i]))
        if d[i] <= GIPFELNAH and (i not in beste or rang < beste[i][0]):
            beste[i] = (rang, h)
    for i, (_, h) in beste.items():
        h["seite"] = 1 if h["lon"] >= gipfel[i]["lon"] else -1
    return len(beste)


PUNKT_ZOOM = {
    "berggasthof": 13, "selbstversorger": 13, "biwak": 13, "einkehr": 14, "unterstand": 15,
    "wasserfall": 13, "station": 13, "quelle": 14, "hoehle": 14, "aussicht": 14, "kapelle": 14,
    "trinkwasser": 15, "kreuz": 15, "marterl": 15, "haltestelle": 15,
}


def mindestzoom(p: dict) -> int:
    k = p["klasse"]
    if k == "huette":
        return 11 if p.get("verein") or p.get("betten", 0) >= 30 else 12
    if k == "sattel":
        return 13 if p["name"] else 14
    if k == "parkplatz":
        return 14 if p.get("wander") else 15  # sonstige Parkplätze im Gebirge (auch im Ort) erst später
    return PUNKT_ZOOM.get(k, 14)


# ---------------------------------------------------------------- Kacheln

def welt(lon: np.ndarray, lat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Web-Mercator in Kachelkoordinaten der höchsten Zoomstufe (EXTENT je Kachel)."""
    n = 2**MAX_ZOOM * EXTENT
    x = (lon + 180) / 360 * n
    y = (1 - np.arcsinh(np.tan(np.radians(lat))) / math.pi) / 2 * n
    return x, y


def linie(koord) -> LineString:
    a = np.asarray(koord)
    x, y = welt(a[:, 0], a[:, 1])
    return LineString(np.column_stack([x, y]))


def wege_mindestzoom(w: dict) -> int:
    if w["netz"] >= FERN:
        return 10
    if w["art"] == "via_ferrata" or w["sac"] >= 2 or w["netz"]:
        return 12
    return 13


BAHN_ZOOM = {"cable_car": 10, "gondola": 10, "goods": 12, "chair_lift": 12, "mixed_lift": 12, "zip_line": 14}


def erzeuge_kacheln(s: Sammler) -> dict[int, bytes]:
    ebenen = {"wege": [], "bahnen": [], "huetten": [], "gipfel": [], "punkte": []}
    for w in s.wege.values():
        eig = {k: v for k, v in w.items() if k != "koord" and v not in ("", None, 0)}
        eig["sac"] = w["sac"]
        eig["sicht"] = w["sicht"]
        eig["netz"] = w["netz"]
        ebenen["wege"].append((linie(w["koord"]), eig, wege_mindestzoom(w)))
    for b in s.bahnen.values():
        eig = {k: v for k, v in b.items() if k != "koord" and v}
        eig["minzoom"] = BAHN_ZOOM.get(b["art"], 13)  # Schlepplifte u. Ä. ab 13
        ebenen["bahnen"].append((linie(b["koord"]), eig, eig["minzoom"]))
    punkte = list(s.punkte.values())
    gipfel_dominanz(punkte)
    for p in punkte:
        x, y = welt(np.array([p["lon"]]), np.array([p["lat"]]))
        eig = {k: v for k, v in p.items() if k not in ("lon", "lat", "minzoom") and v not in ("", None)}
        z = p.get("minzoom") or mindestzoom(p)
        eig["minzoom"] = z
        ziel = "huetten" if p["klasse"] in HUETTEN_KLASSEN else \
            "gipfel" if p["klasse"] == "gipfel" else "punkte"
        ebenen[ziel].append((Point(float(x[0]), float(y[0])), eig, z))

    kacheln: dict[int, bytes] = {}
    for z in ZOOM:
        massstab = 2 ** (MAX_ZOOM - z)
        je_ebene = {}
        for name, objekte in ebenen.items():
            # Mindestzoom über 14: trotzdem in die Kacheln der Zoomstufe 14, der Stil blendet ein.
            auswahl = [o for o in objekte if min(o[2], MAX_ZOOM) <= z]
            geo = [o[0] for o in auswahl]
            if z < MAX_ZOOM and name in ("wege", "bahnen"):
                geo = list(shapely.simplify(geo, massstab * 0.5))
            je_ebene[name] = (shapely.STRtree(geo), geo, [o[1] for o in auswahl])
        anzahl = 0
        for x, y in kacheln_im_gebiet(z):
            x0, y0 = x * EXTENT * massstab, y * EXTENT * massstab
            rand = RAND * massstab
            layers = []
            for name, (baum, geo, eig) in je_ebene.items():
                treffer = baum.query(shapely.box(x0 - rand, y0 - rand, x0 + EXTENT * massstab + rand,
                                                 y0 + EXTENT * massstab + rand))
                features = []
                for i in treffer:
                    g = shapely.clip_by_rect(geo[i], x0 - rand, y0 - rand, x0 + EXTENT * massstab + rand,
                                             y0 + EXTENT * massstab + rand)
                    if g.is_empty:
                        continue
                    g = shapely.transform(g, lambda c: (c - (x0, y0)) / massstab)
                    features.append({"geometry": g, "properties": eig[i]})
                if features:
                    layers.append({"name": name, "features": features})
            if layers:
                mvt = mapbox_vector_tile.encode(layers, default_options={"extents": EXTENT, "y_coord_down": True})
                kacheln[zxy_to_tileid(z, x, y)] = gzip.compress(mvt)
                anzahl += 1
        print(f"  Zoom {z}: {anzahl} Kacheln")
    return kacheln


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pbf", type=Path, nargs="+", required=True)
    parser.add_argument("--hoehen", type=Path, nargs="+", required=True)
    parser.add_argument("--ausgabe", type=Path, required=True)
    parser.add_argument("--gebiet", help="West,Süd,Ost,Nord in Grad (Standard: Kartengebiet des Kerns)")
    parser.add_argument("--region", help="Region der Gebietserweiterung: hängt -<region> an den Dateinamen")
    args = parser.parse_args()
    if args.gebiet:
        gelaende.gebiet_setzen(*map(float, args.gebiet.split(",")))
    anhang = f"-{args.region}" if args.region else ""

    print("1/4 Relevante Objekte herausfiltern")
    gefiltert = []
    for quelle in args.pbf:
        ziel = quelle.with_name(quelle.name.replace(".osm.pbf", f"-wandern{FILTER_VERSION}.osm.pbf"))
        filtern(quelle, ziel)
        gefiltert.append(ziel)

    print("2/4 Routen, Wege und Punkte lesen")
    s = Sammler(Hoehen(args.hoehen))
    for f in gefiltert:
        s.routen_lesen(f)
    for f in gefiltert:
        s.objekte_lesen(f)
    print(f"  {len(s.wege)} Wege, {len(s.bahnen)} Bahnen, {len(s.punkte)} Punkte")
    print("  Wege nach Schwierigkeit:", dict(sorted(Counter(w['sac'] for w in s.wege.values()).items())))
    print("  Wege nach Art:", dict(Counter(w['art'] for w in s.wege.values()).most_common()))
    print("  Wege mit Wegnummer:", sum(1 for w in s.wege.values() if w["ref"]),
          "| in Route:", sum(1 for w in s.wege.values() if w["netz"]),
          "| Fernwanderweg:", sum(1 for w in s.wege.values() if w["netz"] >= FERN),
          "| Klettersteig:", Counter(w["ks"] or "?" for w in s.wege.values() if w["art"] == "via_ferrata"))
    print("  Sichtbarkeit:", dict(sorted(Counter(w['sicht'] for w in s.wege.values()).items())))
    print("  Punkte:", dict(Counter(p['klasse'] for p in s.punkte.values()).most_common()))

    gipfelkreuze_zuordnen(s.punkte)
    print("  Gipfel mit Kreuz:", sum(1 for p in s.punkte.values() if p.get("kreuz")),
          "| Hütten am Gipfel:", huetten_am_gipfel(s.punkte))

    print("3/4 Kacheln erzeugen")
    kacheln = erzeuge_kacheln(s)

    print("4/4 Schreiben")
    schreibe(args.ausgabe / f"mondschein-wandern{anhang}.pmtiles", kacheln, TileType.MVT, Compression.GZIP, ZOOM, {
        "name": "Mondschein Wandern",
        "attribution": "© OpenStreetMap-Mitwirkende (ODbL)",
        "vector_layers": [{"id": n, "fields": {}} for n in ("wege", "bahnen", "huetten", "gipfel", "punkte")],
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
