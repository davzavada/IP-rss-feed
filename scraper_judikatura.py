#!/usr/bin/env python3
"""Sběr judikatury: objeví nová rozhodnutí, AI je shrne a zařadí do oblastí.

Soudy: Nejvyšší soud (všechny senáty), Nejvyšší správní soud a Ústavní
soud; Soudní dvůr EU přibude. Archiv je v data/judikatura/, okna pro web
v docs/data/judikatura/ – viz balíček judikatura/ a README.

Použití:
    python scraper_judikatura.py                  # všechny soudy s adaptérem
    python scraper_judikatura.py --soudy nss,us
    SKIP_GEMINI=1 python scraper_judikatura.py    # jen objevování, bez AI

Rozpočet AI na běh: --max-polozek / --max-minut, nebo proměnné
AI_MAX_POLOZEK a AI_MAX_MINUT (workflow je nastavuje).
"""

import argparse
import os
import sys

from judikatura import orchestr
from judikatura.soudy.ns import NS
from judikatura.soudy.nss import NSS
from judikatura.soudy.us import US

ADAPTERY = {"ns": NS, "nss": NSS, "us": US}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--soudy", default=os.environ.get("SOUDY") or ",".join(ADAPTERY),
                    help="soudy oddělené čárkou (%(default)s)")
    ap.add_argument("--max-polozek", type=int,
                    default=int(os.environ.get("AI_MAX_POLOZEK") or 60))
    ap.add_argument("--max-minut", type=float,
                    default=float(os.environ.get("AI_MAX_MINUT") or 20))
    args = ap.parse_args()

    soudy = [s.strip() for s in args.soudy.split(",") if s.strip()]
    nezname = [s for s in soudy if s not in ADAPTERY]
    if nezname:
        sys.exit(f"Soud bez adaptéru: {', '.join(nezname)} (umím {', '.join(ADAPTERY)})")
    souhrn = orchestr.beh({s: ADAPTERY[s]() for s in soudy}, soudy,
                          max_polozek=args.max_polozek, max_minut=args.max_minut)
    print(f"Hotovo: {souhrn}")


if __name__ == "__main__":
    main()
