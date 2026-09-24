"""Archiv rozhodnutí: data/judikatura/{soud}/ a okna pro web v docs/data/.

  data/judikatura/{soud}/{RRRR-MM}.jsonl   záznamy podle měsíce prvního výskytu,
                                           jeden JSON na řádek (klíče seřazené)
  data/judikatura/{soud}/index.tsv          id<TAB>RRRR-MM za celou historii
  docs/data/judikatura/{soud}.json          okno pro web (výřez polí)

Archiv je zároveň stav běhu – kdo tu je, ten už byl viděn a má (nebo čeká
na) shrnutí. Uzavřený měsíc se už nemění, takže repo roste zhruba o to, co
opravdu přibude. Na web jde jen okno (pár set záznamů na soud).
"""

import json
import os
from datetime import timedelta

import feed_common as fc
from judikatura import fronta, model

KOREN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(KOREN, "data", "judikatura")
WEB_DIR = os.path.join(KOREN, "docs", "data", "judikatura")

# Pole, která jdou na web. Metadata pro AI, stav a detaily AI zůstávají
# v archivu.
SLIM_POLE = ("id", "spz", "ecli", "druh", "senat", "rejstrik", "datum", "zverejneno",
             "first_seen", "nazev", "url", "pdf", "soudce")


def zapis_atomicky(cesta, text):
    """Zapíše soubor přes dočasný soubor – přerušený běh nenechá půlku."""
    os.makedirs(os.path.dirname(cesta), exist_ok=True)
    tmp = cesta + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, cesta)


def radek(zaznam):
    return json.dumps(zaznam, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# Proč u rozhodnutí na webu ještě není shrnutí (kód -> věta u řádku).
STAV_SHRNUTI = {
    "pripravuje": "Shrnutí se připravuje.",
    "ceka_na_text": "Čeká na zveřejnění textu rozhodnutí.",
    "nepodarilo": "Shrnutí se nepodařilo připravit.",
}


# U předběžné otázky se nečeká na text rozhodnutí, ale na položené otázky.
CEKA_NA_OTAZKY = "Položené otázky zatím nejsou zveřejněné."


def stav_shrnuti(z):
    """Kód pro rozhodnutí bez shrnutí: čekání na text od soudu (zkouší se,
    dokud je v okně), vyčerpané pokusy AI, jinak čeká ve frontě na AI."""
    stav = z.get("stav") or {}
    if stav.get("duvod") == "bez-textu":
        return "ceka_na_text"
    if int(stav.get("pokusy") or 0) >= fronta.MAX_POKUSU:
        return "nepodarilo"
    return "pripravuje"


def slim(z):
    """Výřez záznamu pro web: metadata, shrnutí, oblasti, příznak procesní;
    bez shrnutí navíc `stav_shrnuti` a větu k němu v `poznamka`."""
    out = {k: z.get(k) for k in SLIM_POLE if z.get(k) not in (None, "", [])}
    if not out.get("soudce") and (z.get("meta") or {}).get("soudce"):
        out["soudce"] = z["meta"]["soudce"]
    ai = z.get("ai") or {}
    # Právní formy pryč i u shrnutí, která AI napsala dřív (archiv zůstává).
    heslo = ai.get("heslo", "")
    out["heslo"] = fc.bez_pravni_formy(heslo).rstrip(".") if heslo else ""
    out["shrnuti"] = fc.bez_pravni_formy(ai.get("shrnuti", ""))
    out["oblasti"] = ai.get("oblasti") or z.get("oblasti_meta") or []
    procesni = ai.get("procesni")
    out["procesni"] = bool(z.get("procesni_meta") if procesni is None else procesni)
    if not out["shrnuti"]:
        out["stav_shrnuti"] = stav_shrnuti(z)
        out["poznamka"] = STAV_SHRNUTI[out["stav_shrnuti"]]
        if out["stav_shrnuti"] == "ceka_na_text" and z.get("druh") == "předběžná otázka":
            out["poznamka"] = CEKA_NA_OTAZKY
    return out


class Sklad:
    def __init__(self, soud, data_dir=DATA_DIR, web_dir=WEB_DIR):
        self.soud = soud
        self.dir = os.path.join(data_dir, soud)
        self.web = os.path.join(web_dir, f"{soud}.json")
        self.index = {}        # id -> měsíc (celá historie)
        self.zaznamy = {}      # id -> záznam (načtené měsíce)
        self._mesic = {}       # id -> měsíc souboru, kde záznam leží
        self._nacteno = set()  # načtené měsíce
        self._spinave = set()  # měsíce k zápisu
        self._nove_v_indexu = []

    # --- čtení ---

    def nacti(self, nyni=None, mesicu=3):
        """Načte index a záznamy za posledních `mesicu` měsíců (okna, fronta
        AI i dohledání desky NS dál nesahají)."""
        nyni = nyni or model.ted()
        cesta = os.path.join(self.dir, "index.tsv")
        if os.path.exists(cesta):
            with open(cesta, encoding="utf-8") as f:
                for r in f:
                    id_, _, m = r.rstrip("\n").partition("\t")
                    if id_:
                        self.index[id_] = m
        mesice = set()
        d = nyni.replace(day=1)
        for _ in range(mesicu):
            mesice.add(model.mesic(d))
            d = (d - timedelta(days=1)).replace(day=1)
        for m in sorted(mesice):
            self._nacti_mesic(m)
        return self

    def _nacti_mesic(self, m):
        if m in self._nacteno:
            return
        self._nacteno.add(m)
        cesta = os.path.join(self.dir, f"{m}.jsonl")
        if not os.path.exists(cesta):
            return
        with open(cesta, encoding="utf-8") as f:
            for r in f:
                r = r.strip()
                if r:
                    z = json.loads(r)
                    self.zaznamy[z["id"]] = z
                    self._mesic[z["id"]] = m

    def prazdny(self):
        """Soud ještě nemá žádnou historii (první běh = náběh)."""
        return not self.index

    def ma(self, id_):
        return id_ in self.index

    def podle_klice(self, spz_klic):
        """Načtené záznamy se stejnou spisovou značkou (deska NS vs. databáze)."""
        return [z for z in self.zaznamy.values()
                if spz_klic and z.get("spz_klic") == spz_klic]

    # --- zápis ---

    def pridej(self, z):
        """Nový záznam – do měsíce svého prvního výskytu."""
        m = model.mesic(model.z_iso(z["first_seen"]))
        self._nacti_mesic(m)
        self.zaznamy[z["id"]] = z
        self._mesic[z["id"]] = m
        if z["id"] not in self.index:
            self.index[z["id"]] = m
            self._nove_v_indexu.append((z["id"], m))
        self._spinave.add(m)

    def zmeneno(self, z):
        """Záznam z archivu se změnil – jeho měsíc se zapíše znovu."""
        m = self._mesic.get(z["id"])
        if m:
            self._spinave.add(m)

    def uloz(self):
        """Zapíše změněné měsíce a doplní index. Volá se i uprostřed běhu,
        ať ukončený běh nepřijde o hotová shrnutí."""
        for m in sorted(self._spinave):
            # Řádky podle id – pořadí se mezi běhy nemění, v gitu se tak
            # ukazuje jen to, co se doopravdy změnilo.
            radky = [radek(z) for id_, z in sorted(self.zaznamy.items())
                     if self._mesic.get(id_) == m]
            zapis_atomicky(os.path.join(self.dir, f"{m}.jsonl"), "\n".join(radky) + "\n")
        self._spinave.clear()
        if self._nove_v_indexu:
            os.makedirs(self.dir, exist_ok=True)
            with open(os.path.join(self.dir, "index.tsv"), "a", encoding="utf-8") as f:
                for id_, m in self._nove_v_indexu:
                    f.write(f"{id_}\t{m}\n")
            self._nove_v_indexu.clear()

    # --- okno pro web ---

    def v_okne(self, nyni, okno_dni):
        hranice = nyni - timedelta(days=okno_dni)
        out = []
        for z in self.zaznamy.values():
            prvni = model.z_iso(z.get("first_seen"))
            if prvni and prvni >= hranice and not z.get("nahrazeno"):
                out.append(z)
        out.sort(key=lambda z: (z.get("zverejneno") or z.get("first_seen") or "",
                                z.get("first_seen") or "", z["id"]), reverse=True)
        return out

    def exportuj(self, nyni, okno_dni):
        """Zapíše okno pro web. Jen když se obsah opravdu změnil – jinak by
        každý běh měnil čas a spouštěl commit i deploy. Vrací True při zápisu."""
        polozky = [slim(z) for z in self.v_okne(nyni, okno_dni)]
        obsah = {"soud": self.soud, "okno_dni": okno_dni, "polozky": polozky}
        if os.path.exists(self.web):
            try:
                with open(self.web, encoding="utf-8") as f:
                    stary = json.load(f)
                stary.pop("generated", None)
                if stary == obsah:
                    return False
            except (ValueError, OSError):
                pass
        data = {"generated": model.iso(nyni), **obsah}
        zapis_atomicky(self.web, json.dumps(data, ensure_ascii=False, indent=1) + "\n")
        return True
