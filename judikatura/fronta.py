"""Fronta rozhodnutí pro AI a rozpočet jednoho běhu.

Free tier nestihne všechno najednou (úvodní dávka, rušné dny), proto:
  - ve frontě jsou jen rozhodnutí z okna webu – co z okna vypadne bez
    shrnutí, zůstane v archivu bez něj a fronta nemůže donekonečna růst;
  - soudy se střídají, ať jeden nevyčerpá běh ostatním;
  - v rámci soudu jde nejdřív, co podle metadat spadá do výchozího výběru,
    pak věcná rozhodnutí a nakonec procesní; v každé skupině od nejnovějšího;
  - když text zatím není (PDF přikládají soudy s odstupem, žádost
    o předběžnou otázku vyjde týdny po podání), zkouší se znovu po 1, 2,
    4… hodinách a pak při každém běhu, dokud je rozhodnutí v okně;
  - když selže AI, zkusí se to nejvýš šestkrát. Čekání na text se do toho
    nepočítá – má vlastní počítadlo (`pokusy_text`), jen pro odklad.
"""

import time
from datetime import timedelta

from judikatura import model
from judikatura.analyza import PROMPT_VERZE

MAX_POKUSU = 6
# Sběr běží jednou denně; odklad kratší než den zajistí, že se to zkusí
# při každém běhu, i když ten další začne o pár minut dřív.
MAX_ODKLAD_H = 20


def potrebuje_ai(z, nyni):
    if z.get("nahrazeno"):
        return False
    ai = z.get("ai") or {}
    if ai.get("shrnuti") and int(ai.get("pv") or 0) >= PROMPT_VERZE:
        return False
    stav = z.get("stav") or {}
    if pokusy_ai(stav) >= MAX_POKUSU:
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


def pokusy_ai(stav):
    """Kolik pokusů AI rozhodnutí vyčerpalo. Starší záznamy měly jediné
    počítadlo i pro čekání na text – to se za pokusy AI nebere."""
    stav = stav or {}
    if stav.get("duvod") == "bez-textu" and "pokusy_text" not in stav:
        return 0
    return int(stav.get("pokusy") or 0)


def odlozit(z, nyni, duvod):
    """Nepovedlo se (chybí text, AI nedala odpověď) – zkusit později.

    Čekání na text a selhání AI se počítají zvlášť: rozhodnutí, které týdny
    čekalo na text, má pak pořád všech šest pokusů AI."""
    stav = z.setdefault("stav", {})
    if "pokusy_text" not in stav:
        # Starší záznam: jediné počítadlo patřilo čekání na text, když to byl
        # poslední důvod.
        text = stav.get("duvod") == "bez-textu"
        stav["pokusy_text"] = int(stav.get("pokusy") or 0) if text else 0
        stav["pokusy"] = 0 if text else int(stav.get("pokusy") or 0)
    klic = "pokusy_text" if duvod == "bez-textu" else "pokusy"
    stav[klic] = int(stav.get(klic) or 0) + 1
    hodin = min(2 ** (stav[klic] - 1), MAX_ODKLAD_H)
    stav["dalsi_pokus"] = model.iso(nyni + timedelta(hours=hodin))
    stav["duvod"] = duvod


class Rozpocet:
    """Kolik položek a minut smí jeden běh zabrat. Čas se počítá od začátku
    běhu (objevování, AI i přepis hesel), ať celý běh stihne limit kroku
    ve workflow – ten je o rezervu na nejdelší jedno volání AI delší."""

    def __init__(self, max_polozek, max_minut, zacatek=None):
        self.max_polozek = max_polozek
        self.konec = (time.monotonic() if zacatek is None else zacatek) + max_minut * 60
        self.hotovo = 0

    def zbyva(self):
        """Sekund do konce rozpočtu (záporné po něm)."""
        return self.konec - time.monotonic()

    def cas(self, rezerva=0):
        """Zbývá ještě čas (a aspoň `rezerva` sekund navíc)?"""
        return self.zbyva() > rezerva

    def dalsi(self, rezerva=0):
        return self.hotovo < self.max_polozek and self.cas(rezerva)

    def zapocitej(self):
        self.hotovo += 1
