"""Fronta rozhodnutí pro AI a rozpočet jednoho běhu.

Free tier nestihne všechno najednou (úvodní dávka, rušné dny), proto:
  - ve frontě jsou jen rozhodnutí z okna webu – co z okna vypadne bez
    shrnutí, zůstane v archivu bez něj a fronta nemůže donekonečna růst;
  - soudy se střídají, ať jeden nevyčerpá běh ostatním;
  - v rámci soudu jde nejdřív, co podle metadat spadá do výchozího výběru,
    pak věcná rozhodnutí a nakonec procesní; v každé skupině od nejnovějšího;
  - když text zatím není (PDF přikládají soudy s odstupem), zkouší se
    znovu po 1, 2, 4… hodinách, nejvýš šestkrát.
"""

import time
from datetime import timedelta

from judikatura import model
from judikatura.analyza import PROMPT_VERZE

MAX_POKUSU = 6
MAX_ODKLAD_H = 24


def potrebuje_ai(z, nyni):
    if z.get("nahrazeno"):
        return False
    ai = z.get("ai") or {}
    if ai.get("shrnuti") and int(ai.get("pv") or 0) >= PROMPT_VERZE:
        return False
    stav = z.get("stav") or {}
    if int(stav.get("pokusy") or 0) >= MAX_POKUSU:
        return False
    dalsi = model.z_iso(stav.get("dalsi_pokus"))
    return not dalsi or dalsi <= nyni


def trida(z, tax):
    """0 = spadá do výchozího výběru, 1 = věcné, 2 = procesní podle metadat."""
    if z["soud"] == "ns" and z.get("senat") == 23:
        return 0
    if set(z.get("oblasti_meta") or []) & set(tax.vychozi):
        return 0
    return 2 if z.get("procesni_meta") else 1


def sestav(sklady, nyni, tax):
    """Seřazená fronta přes všechny soudy (střídavě)."""
    podle_soudu = []
    for soud, sklad in sklady.items():
        kandidati = [z for z in sklad.v_okne(nyni, model.OKNA_DNI[soud]) if potrebuje_ai(z, nyni)]
        kandidati.sort(key=lambda z: (trida(z, tax),
                                      _zaporne(z.get("zverejneno") or z.get("first_seen") or "")))
        podle_soudu.append(kandidati)
    fronta = []
    for i in range(max((len(k) for k in podle_soudu), default=0)):
        for kandidati in podle_soudu:
            if i < len(kandidati):
                fronta.append(kandidati[i])
    return fronta


def _zaporne(s):
    """Řazení od nejnovějšího v rámci stabilního vzestupného sortu."""
    return tuple(-ord(c) for c in s)


def odlozit(z, nyni, duvod):
    """Nepovedlo se (chybí text, AI nedala odpověď) – zkusit později."""
    stav = z.setdefault("stav", {})
    stav["pokusy"] = int(stav.get("pokusy") or 0) + 1
    hodin = min(2 ** (stav["pokusy"] - 1), MAX_ODKLAD_H)
    stav["dalsi_pokus"] = model.iso(nyni + timedelta(hours=hodin))
    stav["duvod"] = duvod


class Rozpocet:
    """Kolik položek a minut smí AI v jednom běhu zabrat."""

    def __init__(self, max_polozek, max_minut):
        self.max_polozek = max_polozek
        self.konec = time.monotonic() + max_minut * 60
        self.hotovo = 0

    def dalsi(self):
        return self.hotovo < self.max_polozek and time.monotonic() < self.konec

    def zapocitej(self):
        self.hotovo += 1
