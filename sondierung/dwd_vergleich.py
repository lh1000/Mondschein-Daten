"""Vergleich altes gegen neues DWD-Schema (Mondschein, DWD-Umstellung zum 30.11.2026). Ändert nichts.

Für den neuesten Lauf, den beide Schemata haben: Höhen (HHL) und Wolkenanteil (CLC) einiger Flächen in
einer feinen und einer groben Kachel, einmal aus dem regelmäßigen Gitter (alt), einmal aus dem
Dreiecksgitter mit nächster Zelle (neu). Ausgabe: mittlere und größte Abweichung, Korrelation.
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "wetter"))
import icon_d2 as d  # noqa: E402


def main() -> None:
    lauf = d.v1_neuester_lauf()
    alt_lauf = d.neuester_lauf()
    lauf = min(lauf, alt_lauf)
    print(f"Lauf {lauf} (neu {d.v1_neuester_lauf()}, alt {alt_lauf})")
    gitter = d.Dreiecksgitter(lauf)
    print(f"Dreiecksgitter {gitter.anzahl} Zellen")
    ebenen = [e for e in d.kacheln() if (e.schritt == 1 and abs(e.i0 * d.WEITE - 47) < 0.01 and abs(e.j0 * d.WEITE - 11) < 0.01)
              or (e.schritt == 2 and abs(e.i0 * d.WEITE - 44) < 0.05)][:2]
    for e in ebenen:
        idx = gitter.index(e)
        print(f"Kachel {e.i0 * d.WEITE:.2f}/{e.j0 * d.WEITE:.2f} Schritt {e.schritt}: {int((idx < 0).sum())} Knoten außerhalb")
        for param, stunde, k in (("hhl", 0, 60), ("hhl", 0, 40), ("clc", 0, 60), ("clc", 6, 55), ("clc", 12, 45), ("clc", 24, 35)):
            f_alt = d.grib_feld(d.laden(d.datei_url(lauf, param, stunde, k)))
            a = e.ausschneiden(f_alt[0], f_alt[1], f_alt[2], True)
            f_neu = d.grib_werte(d.laden(d.v1_url(param.upper(), lauf, stunde, k)))
            n = e.zusammenfassen(d.auf_ebene(f_neu, idx), True)
            ok = ~np.isnan(a) & ~np.isnan(n)
            diff = np.abs(a[ok] - n[ok])
            korr = np.corrcoef(a[ok], n[ok])[0, 1] if a[ok].std() > 0 and n[ok].std() > 0 else float("nan")
            print(f"  {param} Stunde {stunde:2d} Fläche {k}: alt Mittel {np.nanmean(a):8.1f}, neu {np.nanmean(n):8.1f}, "
                  f"|Δ| Mittel {diff.mean():7.2f}, P95 {np.percentile(diff, 95):7.2f}, max {diff.max():7.2f}, r = {korr:.3f}")


if __name__ == "__main__":
    main()
