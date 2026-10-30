"""Vergleich einer neu gerechneten Höhenkachel mit der veröffentlichten Fassung (Meilenstein 40).

Gibt Verteilung der Differenz neu − alt aus (getrennt nach Flachland und Wald/Ort lässt sich nicht
unterscheiden; Copernicus ist ein Oberflächenmodell, daher erwartet: neu im Mittel einige Meter tiefer)
und die Höhe an bekannten Gipfeln (Maximum im Umkreis von ca. 100 m).

Aufruf (im Workflow „Höhen aufbereiten“):
    python vergleich.py neu.bin [alt.bin]
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

import hoehenmodell as hm

# Bekannte Höhen (amtlich bzw. Gipfelbuch) zur Plausibilität: Name → Breite, Länge, Höhe
PUNKTE = {
    "Feldberg (Schwarzwald)": (47.8742, 8.0043, 1493),
    "Belchen": (47.8225, 7.8331, 1414),
    "Grand Ballon": (47.9014, 7.0997, 1424),
    "Hornisgrinde": (48.6076, 8.2017, 1164),
    "Champ du Feu": (48.3958, 7.2650, 1099),
    "Katzenbuckel": (49.4708, 9.0408, 626),
    "Lemberg (Alb)": (48.1513, 8.7486, 1015),
    "Hohenstaufen": (48.7417, 9.7194, 684),
    "Vaalserberg": (50.7547, 6.0206, 322),
    "Hoge Veluwe Zijpenberg": (52.0369, 5.9592, 100),
    "Wilseder Berg": (53.1667, 9.9500, 169),
    "Hochfelln": (47.7614, 12.5594, 1674),
    "Gaisberg": (47.8050, 13.1117, 1287),
}


def hoehe_bei(ebene: hm.Ebene, breite: float, laenge: float, radius_m: float = 100) -> float | None:
    e = 3600 * ebene.teilung
    z = round((ebene.nord - breite * e) / ebene.schritt)
    s = round((laenge * e - ebene.west) / ebene.schritt)
    zellen_b = max(1, round(radius_m / (30.9 * ebene.schritt_bogensek)))
    zellen_l = max(1, round(radius_m / (30.9 * ebene.schritt_bogensek * np.cos(np.radians(breite)))))
    zeilen, spalten = ebene.hoehen.shape
    if not (0 <= z < zeilen and 0 <= s < spalten):
        return None
    ausschnitt = ebene.meter(slice(max(0, z - zellen_b), z + zellen_b + 1), slice(max(0, s - zellen_l), s + zellen_l + 1))
    return None if np.isnan(ausschnitt).all() else float(np.nanmax(ausschnitt))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    neu = hm.lies(Path(sys.argv[1]), Path("vergleich-neu"))[0]
    alt = hm.lies(Path(sys.argv[2]), Path("vergleich-alt"))[0] if len(sys.argv) > 2 and Path(sys.argv[2]).exists() else None
    print(f"Vergleich {sys.argv[1]}")
    for name, (b, l, h) in PUNKTE.items():
        n = hoehe_bei(neu, b, l)
        if n is None:
            continue
        a = hoehe_bei(alt, b, l) if alt is not None else None
        print(f"  {name}: Soll {h} m, neu {n:.1f} m" + (f", bisher {a:.1f} m" if a is not None else ""))
    if alt is not None and alt.hoehen.shape == neu.hoehen.shape and (alt.nord, alt.west) == (neu.nord, neu.west):
        d = (neu.meter() - alt.meter())[::7, ::7]
        d = d[np.isfinite(d)]
        q = np.percentile(d, [5, 25, 50, 75, 95])
        print(f"  neu − bisher (jede 7. Zelle, {d.size} Werte): Mittel {d.mean():+.1f} m, Quantile 5/25/50/75/95 % "
              + " / ".join(f"{x:+.1f}" for x in q) + f" m, |Δ| > 30 m: {100 * (np.abs(d) > 30).mean():.2f} %")
    elif alt is not None:
        print("  bisherige Fassung hat ein anderes Raster – kein Flächenvergleich")
    return 0


if __name__ == "__main__":
    sys.exit(main())
