"""Seznam oblastí (docs/data/oblasti.json): jediný zdroj pro AI i web."""

import json
import os
import re

from judikatura.model import bez_diakritiky

KOREN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOUBOR = os.path.join(KOREN, "docs", "data", "oblasti.json")
MAX_OBLASTI = 3


class Taxonomie:
    def __init__(self, cesta=SOUBOR):
        with open(cesta, encoding="utf-8") as f:
            data = json.load(f)
        self.verze = data.get("verze", 1)
        self.oblasti = data["oblasti"]
        self.ids = [o["id"] for o in self.oblasti]
        self.alias = dict(data.get("alias") or {})
        self.vychozi = [o["id"] for o in self.oblasti if o.get("vychozi")]
        # Pro čtení odpovědí AI: id, název i alias bez diakritiky a velikosti.
        self._klice = {}
        for o in self.oblasti:
            for tvar in (o["id"], o["nazev"]):
                self._klice[self._norm(tvar)] = o["id"]
        for stare, nove in self.alias.items():
            self._klice[self._norm(stare)] = nove

    @staticmethod
    def _norm(s):
        return re.sub(r"[\s_\-]+", "_", bez_diakritiky(str(s)).strip().lower())

    def id_z(self, token):
        """Id oblasti z id, názvu nebo aliasu; None, když ho neznáme."""
        return self._klice.get(self._norm(token))

    def normalizuj(self, seznam):
        """Platná id bez duplicit, nejvýš tři. Neznámá id (i dřívější
        „ostatni") se zahodí."""
        out = []
        for token in seznam or []:
            oid = self.id_z(token)
            if oid and oid not in out:
                out.append(oid)
        return out[:MAX_OBLASTI]

    def do_promptu(self):
        """Řádky „id – název: popis" pro prompt."""
        return "\n".join(f"{o['id']} – {o['nazev']}: {o['popis']}" for o in self.oblasti)
