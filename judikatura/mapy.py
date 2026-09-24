"""První zařazení do oblastí podle úředních údajů (data/judikatura/mapy/).

Než rozhodnutí projde AI, řadí se podle toho, co o něm říká soud: oblast
úpravy u NSS, dotčené předpisy, správní orgán, věcný rejstřík ÚS. Výsledek
(`oblasti_meta`) určuje pořadí ve frontě AI, na webu platí do doby, než
AI rozhodne, a zůstane, když AI žádnou oblast nevrátí.

  predpisy.json   číslo předpisu („121/2000") -> oblasti; obecné kodexy
                  (o. s. ř., s. ř. s., občanský zákoník, Listina…) chybí
                  schválně – o věci nic neřeknou
  nss.json        oblast úpravy (přesně, jinak podle části před pomlčkou)
                  a správní orgány (podle části názvu)
  us.json         části slov z věcného rejstříku a předmětu řízení
  ipcuria.json    kategorie ipcuria.eu (rané předběžné otázky SDEU z IP)
"""

import functools
import json
import os
import re

from judikatura.taxonomie import MAX_OBLASTI

KOREN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAPY_DIR = os.path.join(KOREN, "data", "judikatura", "mapy")

# „121/2000 Sb.", „zákona č. 121/2000", „2/1993 Sb./Sb.m.s." -> „121/2000"
_PREDPIS_RE = re.compile(r"\b(\d{1,4})\s*/\s*(\d{4})\b")


@functools.lru_cache(maxsize=None)
def nacti(nazev, adresar=MAPY_DIR):
    with open(os.path.join(adresar, f"{nazev}.json"), encoding="utf-8") as f:
        return json.load(f)


def spoj(*seznamy):
    """Oblasti bez opakování, v pořadí důležitosti, nejvýš tři."""
    out = []
    for seznam in seznamy:
        for o in seznam or []:
            if o not in out:
                out.append(o)
    return out[:MAX_OBLASTI]


def cisla_predpisu(texty):
    """Čísla předpisů („121/2000") z citací, v pořadí výskytu."""
    out = []
    for t in texty or []:
        for m in _PREDPIS_RE.finditer(t or ""):
            klic = f"{int(m.group(1))}/{m.group(2)}"
            if klic not in out:
                out.append(klic)
    return out


def z_predpisu(texty):
    mapa = nacti("predpisy")["predpisy"]
    return spoj(*((mapa.get(k) or {}).get("oblasti") for k in cisla_predpisu(texty)))


def z_oblasti_upravy(oblast):
    """Oblast úpravy NSS: přesná shoda, jinak skupina před pomlčkou
    („Daně - daň z příjmů" -> „Daně")."""
    mapa = {" ".join(k.split()): v for k, v in nacti("nss")["oblast_upravy"].items()}
    oblast = " ".join((oblast or "").split())
    if oblast in mapa:
        return spoj(mapa[oblast])
    skupina = re.split(r"\s+[-–]\s+", oblast, maxsplit=1)[0]
    return spoj(mapa.get(skupina))


def _podle_casti(mapa, texty):
    texty = [t.lower() for t in texty or [] if t]
    return spoj(*(oblasti for cast, oblasti in mapa.items()
                  if any(cast.lower() in t for t in texty)))


def z_organu(texty):
    """Správní orgán nebo účastník řízení (ÚPV, ÚOOÚ, ČTÚ…)."""
    return _podle_casti(nacti("nss")["organy"], texty)


def z_rejstriku(texty):
    """Věcný rejstřík a předmět řízení ÚS."""
    return _podle_casti(nacti("us")["rejstrik"], texty)


def z_ipcurie(kategorie):
    """Kategorie předběžné otázky na ipcuria.eu („Trade marks > …")."""
    return _podle_casti(nacti("ipcuria")["kategorie"], kategorie)
