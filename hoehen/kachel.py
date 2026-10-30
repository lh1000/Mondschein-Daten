"""Höhenkachel 1° × 1° für die Gebietserweiterung (docs/konzept.md, Meilenstein 27 / E2).

Schreibt `mondschein-hoehen-N47E010.bin` im Format der Höhendatei (README.md) mit einer Ebene:

- südlich von 48° N (Alpen und Voralpen) 1/3″ (ca. 10 m × 7 m), nördlich 1″ (Hügelland, Entscheidung
  des Nutzers vom 28.10.2026);
- Mittelwert der amtlichen Laserscan-Modelle wie im Kerngebiet: BEV ALS DTM (Österreich), Bayern DGM1,
  swissALTI3D (Schweiz, Liechtenstein), seit Meilenstein 40 auch LGL DGM1 (Baden-Württemberg), IGN RGE ALTI
  (Elsass), AHN4 (Niederlande) und LGLN DGM1 (Niedersachsen) (amtlich.py); was keine dieser Quellen
  abdeckt (z. B. Rheinland-Pfalz, Hessen, Bremen, Tschechien), füllt Copernicus GLO-30;
- die Kachel reicht von Grad- zu Gradlinie einschließlich beider Ränder, Nachbarkacheln überlappen also
  um eine Zelle (sonst fiele die App an den Nähten auf den groben Puffer zurück); begrenzt auf das
  Gesamtgebiet in tools/gebiete/gebiete.json.

Mit --puffer <gesamtgebiet> statt --kachel: der grobe Horizontpuffer `mondschein-hoehen-puffer-<id>.bin`
(3″, Maximum je Zelle aus Copernicus) um ein Gesamtgebiet (z. B. „sued“, „kueste“) plus 150 km.

Aufruf (in GitHub Actions: Workflow „Höhen aufbereiten“ im Daten-Repo):
    python kachel.py --kachel N47E010 --ausgabe mondschein-hoehen-N47E010.bin
    python kachel.py --puffer sued --ausgabe mondschein-hoehen-puffer-sued.bin
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from pathlib import Path

import numpy as np
from numpy.lib.format import open_memmap

import amtlich
import hoehenmodell as hm

GEBIETE = Path(__file__).resolve().parent.parent / "gebiete" / "gebiete.json"
PUFFER_KM = 150.0


def gebiete(pfad: Path = GEBIETE) -> dict:
    return json.loads(pfad.read_text(encoding="utf-8"))


def gesamtgebiete(g: dict) -> list[dict]:
    return g["gesamt"] if isinstance(g["gesamt"], list) else [g["gesamt"]]


def kachelgebiet(name: str, g: dict) -> tuple[hm.Gebiet, int]:
    """Gebiet der Kachel (auf das Gesamtgebiet begrenzt) und Teilung (3 = 1/3″, 1 = 1″)."""
    m = re.fullmatch(r"N(\d\d)E(\d\d\d)", name)
    if not m:
        raise SystemExit(f"Kachelname wie N47E010 erwartet, nicht {name}")
    breite, laenge = int(m[1]), int(m[2])
    # Mehrere Gesamtgebiete (Süddeutschland, Küste): das, das die Kachel schneidet
    for gesamt in gesamtgebiete(g):
        gebiet = hm.Gebiet(sued=max(breite, gesamt["sued"]), nord=min(breite + 1, gesamt["nord"]),
                           west=max(laenge, gesamt["west"]), ost=min(laenge + 1, gesamt["ost"]))
        if gebiet.sued < gebiet.nord and gebiet.west < gebiet.ost:
            break
    else:
        raise SystemExit(f"{name} liegt außerhalb der Gesamtgebiete")
    return gebiet, 3 if breite < g["fein_bis"] else 1


ALLE_QUELLEN = "bev,bayern,schweiz,bw,elsass,niederlande,niedersachsen"


def kachel(name: str, ausgabe: Path, arbeitsordner: Path, cache: Path, quellen: set[str] = frozenset(ALLE_QUELLEN.split(","))) -> None:
    g = gebiete()
    gebiet, teilung = kachelgebiet(name, g)
    faktor = 10
    nord, sued, west, ost = hm.raster(gebiet, 1, teilung)
    gitter = amtlich.Gitter(nord, west, 1, teilung, nord - sued + 1, ost - west + 1)
    print(f"{name}: {gebiet}, {gitter.zeilen} × {gitter.spalten} Punkte ({'1/3' if teilung == 3 else '1'}″), "
          f"Platz frei {amtlich.freier_platz(arbeitsordner):.0f} GB")
    summen = amtlich.Summen(arbeitsordner, name, gitter)
    if "bev" in quellen:
        amtlich.oesterreich(summen, aufloesung=2 if teilung == 3 else 8)
    if "bayern" in quellen:
        amtlich.bayern(summen, arbeitsordner)
    if "schweiz" in quellen:
        amtlich.schweiz(summen)
    if "bw" in quellen:
        amtlich.baden_wuerttemberg(summen, aufloesung=2 if teilung == 3 else 5)
    if "elsass" in quellen:
        amtlich.elsass(summen, proben=2 if teilung == 1 else 1)
    if "niederlande" in quellen:
        amtlich.niederlande(summen)
    if "niedersachsen" in quellen:
        amtlich.niedersachsen(summen, aufloesung=2 if teilung == 3 else 5)
    hoehen = open_memmap(arbeitsordner / f"{name}_dm.npy", mode="w+", dtype=np.int16, shape=(gitter.zeilen, gitter.spalten))
    luecken = amtlich.mittelwert_in_bloecken(summen, hoehen, faktor)
    gesamt = gitter.zeilen * gitter.spalten
    print(f"  amtlich abgedeckt: {100 * (1 - luecken / gesamt):.1f} %, Rest aus Copernicus")
    e = hm.Ebene(nord, west, 1, hoehen, teilung, faktor)
    hm.fuelle_luecken(e, hm.Mosaik(cache))
    hoehen.flush()
    hm.schreibe(ausgabe, [e])


def puffer(gid: str, ausgabe: Path, cache: Path) -> None:
    gesamt = next((x for x in gesamtgebiete(gebiete()) if x.get("id") == gid), None)
    if gesamt is None:
        raise SystemExit(f"Gesamtgebiet {gid} fehlt in {GEBIETE}")
    nord = gesamt["nord"] + PUFFER_KM / 111.2
    km_je_grad = 111.2 * math.cos(math.radians(nord))
    gebiet = hm.Gebiet(sued=gesamt["sued"] - PUFFER_KM / 111.2, nord=nord,
                       west=gesamt["west"] - PUFFER_KM / km_je_grad, ost=gesamt["ost"] + PUFFER_KM / km_je_grad)
    print(f"Puffer {gebiet}")
    hm.schreibe(ausgabe, [hm.puffer_ebene(hm.Mosaik(cache), gebiet=gebiet)])


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    art = p.add_mutually_exclusive_group(required=True)
    art.add_argument("--kachel", help="z. B. N47E010 (Südwestecke)")
    art.add_argument("--puffer", metavar="GESAMTGEBIET", help="3″-Horizontpuffer um ein Gesamtgebiet (sued, kueste)")
    p.add_argument("--ausgabe", type=Path, required=True)
    p.add_argument("--arbeitsordner", type=Path, default=Path("arbeit"))
    p.add_argument("--cache", type=Path, default=Path("copernicus"))
    p.add_argument("--quellen", default=ALLE_QUELLEN, help="amtliche Quellen, z. B. bev,schweiz (Rest: Copernicus)")
    a = p.parse_args()
    beginn = time.time()
    if a.puffer:
        puffer(a.puffer, a.ausgabe, a.cache)
    else:
        kachel(a.kachel, a.ausgabe, a.arbeitsordner, a.cache, set(a.quellen.split(",")))
    print(f"fertig nach {(time.time() - beginn) / 60:.0f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
