"""Höhenkachel 1° × 1° für die Gebietserweiterung (docs/konzept.md, Meilenstein 27 / E2).

Schreibt `mondschein-hoehen-N47E010.bin` im Format der Höhendatei (README.md) mit einer Ebene:

- südlich von 48° N (Alpen und Voralpen) 1/3″ (ca. 10 m × 7 m), nördlich 1″ (Hügelland, Entscheidung
  des Nutzers vom 28.10.2026);
- Mittelwert der amtlichen Laserscan-Modelle wie im Kerngebiet: BEV ALS DTM (Österreich), Bayern DGM1
  und swissALTI3D (Schweiz, Liechtenstein; amtlich.py); was keine dieser Quellen abdeckt (vorerst
  Baden-Württemberg und Elsass), füllt Copernicus GLO-30;
- die Kachel reicht von Grad- zu Gradlinie einschließlich beider Ränder, Nachbarkacheln überlappen also
  um eine Zelle (sonst fiele die App an den Nähten auf den groben Puffer zurück); begrenzt auf das
  Gesamtgebiet in tools/gebiete/gebiete.json.

Mit --puffer statt --kachel: der gemeinsame grobe Horizontpuffer `mondschein-hoehen-puffer.bin`
(3″, Maximum je Zelle aus Copernicus) um das Gesamtgebiet plus 150 km.

Aufruf (in GitHub Actions: Workflow „Höhen aufbereiten“ im Daten-Repo):
    python kachel.py --kachel N47E010 --ausgabe mondschein-hoehen-N47E010.bin
    python kachel.py --puffer --ausgabe mondschein-hoehen-puffer.bin
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


def kachel(name: str, ausgabe: Path, arbeitsordner: Path, cache: Path) -> None:
    g = gebiete()
    gebiet, teilung = kachelgebiet(name, g)
    faktor = 10
    nord, sued, west, ost = hm.raster(gebiet, 1, teilung)
    gitter = amtlich.Gitter(nord, west, 1, teilung, nord - sued + 1, ost - west + 1)
    print(f"{name}: {gebiet}, {gitter.zeilen} × {gitter.spalten} Punkte ({'1/3' if teilung == 3 else '1'}″), "
          f"Platz frei {amtlich.freier_platz(arbeitsordner):.0f} GB")
    summen = amtlich.Summen(arbeitsordner, name, gitter)
    amtlich.oesterreich(summen, aufloesung=2 if teilung == 3 else 8)
    amtlich.bayern(summen, arbeitsordner)
    amtlich.schweiz(summen)
    hoehen = open_memmap(arbeitsordner / f"{name}_dm.npy", mode="w+", dtype=np.int16, shape=(gitter.zeilen, gitter.spalten))
    luecken = amtlich.mittelwert_in_bloecken(summen, hoehen, faktor)
    gesamt = gitter.zeilen * gitter.spalten
    print(f"  amtlich abgedeckt: {100 * (1 - luecken / gesamt):.1f} %, Rest aus Copernicus")
    e = hm.Ebene(nord, west, 1, hoehen, teilung, faktor)
    hm.fuelle_luecken(e, hm.Mosaik(cache))
    hoehen.flush()
    hm.schreibe(ausgabe, [e])


def puffer(ausgabe: Path, cache: Path) -> None:
    alle = gesamtgebiete(gebiete())
    gesamt = {"sued": min(x["sued"] for x in alle), "nord": max(x["nord"] for x in alle),
              "west": min(x["west"] for x in alle), "ost": max(x["ost"] for x in alle)}
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
    art.add_argument("--puffer", action="store_true", help="gemeinsamer 3″-Horizontpuffer")
    p.add_argument("--ausgabe", type=Path, required=True)
    p.add_argument("--arbeitsordner", type=Path, default=Path("arbeit"))
    p.add_argument("--cache", type=Path, default=Path("copernicus"))
    a = p.parse_args()
    beginn = time.time()
    if a.puffer:
        puffer(a.ausgabe, a.cache)
    else:
        kachel(a.kachel, a.ausgabe, a.arbeitsordner, a.cache)
    print(f"fertig nach {(time.time() - beginn) / 60:.0f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
