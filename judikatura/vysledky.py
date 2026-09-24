"""Výsledek rozhodnutí: odmítnuto, zamítnuto, zrušeno a vráceno…

Na webu stojí před shrnutím. NSS a ÚS ho vedou v úředních údajích
(meta.vyrok, u ÚS víc hodnot oddělených středníkem), NS a SDEU ne – tam ho
AI čte z textu rozhodnutí spolu se shrnutím (analyza.py, údaj VÝSLEDEK).
Úřední výrok má přednost. Kód `jine` (výklad v řízení o předběžné otázce,
stanovisko GA, přikázání věci, podjatost, odkladný účinek…) štítek nemá.
"""

import re

from judikatura.model import bez_diakritiky

KODY = ("odmitnuto", "zamitnuto", "zruseno_vraceno", "zruseno", "zmeneno", "vyhoveno",
        "castecne", "zastaveno", "jine")

# Soudy, u kterých výsledek určuje AI – úřední údaje ho nemají.
Z_AI = {"ns", "sdeu"}
# Stanovisko GA a žádost o předběžnou otázku o ničem nerozhodují.
BEZ_VYSLEDKU_SDEU = {"stanovisko GA", "předběžná otázka"}

# Odpověď AI: kód, nebo jeho české znění (bez diakritiky, malými písmeny).
_ZNENI = {
    "odmitnuto": "odmitnuto", "zamitnuto": "zamitnuto",
    "zruseno vraceno": "zruseno_vraceno", "zruseno a vraceno": "zruseno_vraceno",
    "zruseno": "zruseno", "zmeneno": "zmeneno", "vyhoveno": "vyhoveno",
    "castecne": "castecne", "castecne vyhoveno": "castecne", "zcasti vyhoveno": "castecne",
    "zastaveno": "zastaveno", "jine": "jine", "jiny": "jine", "jiny vysledek": "jine",
    "bez vysledku": "jine", "zadny": "jine",
}


def normalizuj(hodnota):
    """Kód výsledku z odpovědi AI; None, když odpověď žádnému nesedí."""
    s = bez_diakritiky(str(hodnota or "")).lower()
    s = re.sub(r"[\s_\-–]+", " ", s).strip(" .,*\"'")
    return _ZNENI.get(s)


def _nss(vyrok):
    v = vyrok.lower()
    if v.startswith("zrušeno a vráceno"):
        return "zruseno_vraceno"
    if v.startswith("zrušeno"):          # „zrušeno + zrušení rozhodnutí spr. orgánu" apod.
        return "zruseno"
    if v.startswith("zamítnuto"):
        return "zamitnuto"
    if v.startswith("odmítnuto"):        # i „odmítnuto pro nepřijatelnost"
        return "odmitnuto"
    if v.startswith(("zastaveno", "řízení: zastavení")):
        return "zastaveno"
    return "jine"                        # odkladný účinek, podjatost, rozšířený senát…


def _us(vyrok):
    casti = [c.strip().lower() for c in vyrok.split(";") if c.strip()]
    vecne = [c for c in casti if not c.startswith("procesní")]
    if not vecne:
        return "jine"                    # spojení věcí, ustanovení opatrovníka…
    if any(c.startswith("vyhověno") for c in vecne):
        return "vyhoveno" if all(c.startswith("vyhověno") for c in vecne) else "castecne"
    if any(c.startswith("zamítnuto") for c in vecne):
        return "zamitnuto"
    if any(c.startswith("odmítnuto") for c in vecne):
        return "odmitnuto"
    if all(c.startswith("zastaveno") for c in vecne):
        return "zastaveno"
    return "jine"


def z_uredniho(z):
    """Kód z úředního výroku (NSS, ÚS); None, když ho soud nevede nebo chybí."""
    vyrok = str((z.get("meta") or {}).get("vyrok") or "").strip()
    if not vyrok:
        return None
    if z["soud"] == "nss":
        return _nss(vyrok)
    if z["soud"] == "us":
        return _us(vyrok)
    return None


def bez_vysledku(z):
    return z["soud"] == "sdeu" and z.get("druh") in BEZ_VYSLEDKU_SDEU


def chybi_vysledek(z):
    """Hotové shrnutí z doby, kdy AI výsledek ještě neurčovala, u soudu, kde
    ho jinak odkud vzít není – rozbor se kvůli němu zopakuje."""
    return (z["soud"] in Z_AI and "vysledek" not in (z.get("ai") or {})
            and not bez_vysledku(z))


def vysledek(z):
    """(kód, úřední znění) pro web; ("", "") když štítek nemá být.
    U výsledku od AI je úřední znění prázdné."""
    kod = z_uredniho(z)
    popis = str(z["meta"]["vyrok"]).strip() if kod else ""
    if kod is None:
        if bez_vysledku(z):
            return "", ""
        kod = (z.get("ai") or {}).get("vysledek")
    if kod not in KODY or kod == "jine":
        return "", ""
    return kod, popis
