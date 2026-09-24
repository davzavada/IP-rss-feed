"""Kontrola archivu a oken pro web – před commitem (judikatura.yml) i v CI.

Chyby jsou to, co by rozbilo web nebo další běh: nečitelný řádek, záznam
bez id, totéž id ve dvou měsících, okno pro web, které nejde přečíst nebo
ukazuje nahrazený či neznámý záznam. S chybou se necommituje – další běh
začne z posledního dobrého stavu.

Varování jsou nesrovnalosti, které další běh sám srovná nebo které web
přežije (index po přerušeném běhu, oblast mimo seznam po přejmenování).

    python -m judikatura.kontrola            # všechny soudy, kód 1 při chybě
"""

import json
import os
import re
import sys

from judikatura import model
from judikatura.sklad import DATA_DIR, WEB_DIR
from judikatura.taxonomie import Taxonomie

MESIC_RE = re.compile(r"^\d{4}-\d{2}\.jsonl$")


def _index(adr, soud, chyby, varovani):
    index = {}
    cesta = os.path.join(adr, "index.tsv")
    if not os.path.exists(cesta):
        return index
    with open(cesta, encoding="utf-8") as f:
        for i, r in enumerate(f, 1):
            id_, _, m = r.rstrip("\n").partition("\t")
            if not id_ or not m:
                chyby.append(f"{soud}/index.tsv:{i}: řádek bez id nebo měsíce")
            elif id_ in index:
                varovani.append(f"{soud}/index.tsv:{i}: {id_} je v indexu dvakrát")
            else:
                index[id_] = m
    return index


def _archiv(adr, soud, tax, chyby, varovani):
    """id -> (měsíc, záznam) ze všech měsíčních souborů."""
    zaznamy = {}
    for jmeno in sorted(os.listdir(adr)) if os.path.isdir(adr) else []:
        if not jmeno.endswith(".jsonl"):
            continue
        if not MESIC_RE.match(jmeno):
            chyby.append(f"{soud}/{jmeno}: soubor mimo tvar RRRR-MM.jsonl")
            continue
        m, predchozi = jmeno[:-6], ""
        with open(os.path.join(adr, jmeno), encoding="utf-8") as f:
            for i, r in enumerate(f, 1):
                misto = f"{soud}/{jmeno}:{i}"
                if not r.strip():
                    continue
                try:
                    z = json.loads(r)
                except ValueError:
                    chyby.append(f"{misto}: nečitelný JSON")
                    continue
                id_ = z.get("id") if isinstance(z, dict) else None
                if not id_:
                    chyby.append(f"{misto}: záznam bez id")
                    continue
                if id_ in zaznamy:
                    chyby.append(f"{misto}: {id_} už je v {zaznamy[id_][0]}")
                    continue
                zaznamy[id_] = (m, z)
                if z.get("soud") != soud:
                    chyby.append(f"{misto}: {id_} patří soudu {z.get('soud')!r}")
                prvni = model.z_iso(z.get("first_seen"))
                if not prvni:
                    chyby.append(f"{misto}: {id_} nemá čitelný first_seen")
                elif model.mesic(prvni) != m:
                    varovani.append(f"{misto}: {id_} má first_seen {z['first_seen']} mimo {m}")
                if id_ <= predchozi:
                    varovani.append(f"{misto}: řádky nejsou seřazené podle id")
                predchozi = id_
                for o in (z.get("ai") or {}).get("oblasti") or []:
                    if o not in tax.ids:
                        varovani.append(f"{misto}: {id_} má neznámou oblast {o!r}")
    return zaznamy


def _okno(web, soud, zaznamy, tax, chyby, varovani):
    if not os.path.exists(web):
        return
    try:
        with open(web, encoding="utf-8") as f:
            data = json.load(f)
    except ValueError:
        chyby.append(f"{soud}.json: nečitelný JSON")
        return
    if not isinstance(data, dict) or not isinstance(data.get("polozky"), list):
        chyby.append(f"{soud}.json: chybí seznam položek")
        return
    if data.get("soud") != soud:
        chyby.append(f"{soud}.json: patří soudu {data.get('soud')!r}")
    if not model.z_iso(data.get("generated")):
        chyby.append(f"{soud}.json: chybí čas vygenerování")
    videne = set()
    for p in data["polozky"]:
        id_ = p.get("id") if isinstance(p, dict) else None
        if not id_ or id_ in videne:
            chyby.append(f"{soud}.json: položka bez id nebo dvakrát ({id_})")
            continue
        videne.add(id_)
        z = zaznamy.get(id_, (None, None))[1]
        if z is None:
            chyby.append(f"{soud}.json: {id_} není v archivu")
        elif z.get("nahrazeno"):
            chyby.append(f"{soud}.json: {id_} je nahrazený ({z['nahrazeno']})")
        if not isinstance(p.get("oblasti"), list) or not isinstance(p.get("procesni"), bool):
            chyby.append(f"{soud}.json: {id_} nemá oblasti nebo příznak procesní")
            continue
        nezname = [o for o in p["oblasti"] if o not in tax.ids]
        if nezname:
            varovani.append(f"{soud}.json: {id_} má neznámé oblasti {nezname}")


def zkontroluj(soudy=model.SOUDY, data_dir=DATA_DIR, web_dir=WEB_DIR, tax=None):
    """Vrací (chyby, varování) pro vybrané soudy."""
    tax = tax or Taxonomie()
    chyby, varovani = [], []
    for soud in soudy:
        adr = os.path.join(data_dir, soud)
        index = _index(adr, soud, chyby, varovani)
        zaznamy = _archiv(adr, soud, tax, chyby, varovani)
        for id_, (m, _) in zaznamy.items():
            if index.get(id_) != m:
                varovani.append(f"{soud}: {id_} ({m}) v indexu chybí nebo má jiný měsíc")
        for id_ in index.keys() - zaznamy.keys():
            varovani.append(f"{soud}: {id_} je v indexu, ale v archivu ne")
        _okno(os.path.join(web_dir, f"{soud}.json"), soud, zaznamy, tax, chyby, varovani)
    return chyby, varovani


def main():
    chyby, varovani = zkontroluj()
    for v in varovani:
        print(f"::warning::{v}" if os.environ.get("GITHUB_ACTIONS") else f"varování: {v}")
    for c in chyby:
        print(f"::error::{c}" if os.environ.get("GITHUB_ACTIONS") else f"CHYBA: {c}")
    print(f"Kontrola judikatury: {len(chyby)} chyb, {len(varovani)} varování")
    sys.exit(1 if chyby else 0)


if __name__ == "__main__":
    main()
