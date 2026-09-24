"""AI rozbor rozhodnutí: heslo, shrnutí, oblasti, příznak procesní a výsledek.

Jedno volání na rozhodnutí, vždy nad celým textem. Modely Gemini dostanou
JSON schéma (oblasti a výsledek jako výčet povolených hodnot), Gemma stejné
pokyny a odpovídá značkami HESLO / SHRNUTÍ / OBLASTI / PROCESNÍ / VÝSLEDEK.
Parser zvládne obojí; oblasti mimo seznam zahodí, výsledek převede na kód
(judikatura/vysledky.py).
"""

import base64
import json
import re

import feed_common as fc
from judikatura import vysledky
from judikatura.model import NAZVY_SOUDU

# Zvýšit, když se změní prompt tak, že stará shrnutí už neodpovídají –
# rozhodnutí v okně se pak přepočítají (archiv se sám nepřepisuje).
PROMPT_VERZE = 1
MIN_SHRNUTI = 40

SYSTEM = (
    "Jsi asistent českého advokáta. Dostaneš jedno soudní rozhodnutí (případně "
    "stanovisko generálního advokáta nebo žádost o rozhodnutí o předběžné "
    "otázce) i s úředními údaji. Odpovídej česky, i když je text v jiném "
    "jazyce. Nic si nevymýšlej, drž se textu.\n\n"
    "Vrať pět údajů:\n"
    "HESLO – výstižné právní téma o 1–3 slovech (např. Smluvní pokuta, "
    "Ochranná známka, Přípustnost dovolání).\n"
    "SHRNUTÍ – nejvýše tři věty podle pokynu u rozhodnutí. Právnické osoby "
    "a úřady uváděj jménem, ale bez právní formy (bez s.r.o., a.s. apod.); "
    "fyzické osoby nejmenuj, piš žalobce, žalovaný, stěžovatel, obviněný.\n"
    "OBLASTI – jeden až tři identifikátory ze seznamu níže podle věcné "
    "podstaty sporu, ne podle procesního rámce; první je hlavní. Vždy vyber "
    "aspoň jednu – když žádná nesedí přesně, tu nejbližší.\n"
    "PROCESNÍ – ano jen tehdy, když rozhodnutí nemá věcný právní závěr "
    "(odmítnutí pro vady, opožděnost nebo nepřípustnost bez věcného "
    "posouzení, zastavení řízení, přikázání věci, příslušnost, podjatost, "
    "poplatky, ustanovení zástupce); jinak ne.\n"
    "VÝSLEDEK – jak soud o podání rozhodl, jedním z kódů: odmitnuto, "
    "zamitnuto, zruseno_vraceno (zrušil a vrátil k dalšímu řízení, u trestních "
    "věcí i přikázal věc znovu projednat), zruseno (zrušil bez vrácení – třeba "
    "i rozhodnutí správního orgánu, nebo napadený akt), zmeneno (sám rozhodl "
    "jinak), vyhoveno, castecne (zčásti vyhověl, zčásti ne), zastaveno, jine "
    "(výklad v řízení o předběžné otázce, stanovisko GA, žádost o předběžnou "
    "otázku, přikázání věci, příslušnost, podjatost, odklad a jiná rozhodnutí "
    "bez výsledku ve věci).\n\n"
    "Když dotaz chce JSON, použij pole heslo, shrnuti, oblasti, procesni "
    "a vysledek. Jinak odpověz přesně takto, bez dalšího textu:\n"
    "HESLO: …\nSHRNUTÍ: …\nOBLASTI: id, id\nPROCESNÍ: ano/ne\nVÝSLEDEK: kód\n\n"
    "Seznam oblastí (id – název: co sem patří):\n{oblasti}"
)

POKYNY = {
    "obecny": "V první větě kdo se s kým soudil a o co šlo, pak jakou právní "
              "otázku soud řešil a jak ji vyřešil – konkrétní právní závěr.",
    "us": "Co stěžovatel napadal, zda Ústavní soud shledal porušení kterého "
          "základního práva a proč; u odmítnutí stručně důvod.",
    "sdeu": "Jakou otázku Soudní dvůr (Tribunál) řešil a jak ji zodpověděl – "
            "konkrétní závěr.",
    "stanovisko": "Jakou odpověď generální advokát navrhuje a proč.",
    "otazka": "Na co se předkládající soud (a ze které země) Soudního dvora ptá.",
}


def pokyn(z):
    druh = (z.get("druh") or "").lower()
    if z["soud"] == "sdeu":
        if "stanovisko" in druh:
            return POKYNY["stanovisko"]
        if "otázk" in druh or "otazk" in druh:
            return POKYNY["otazka"]
        return POKYNY["sdeu"]
    if z["soud"] == "us":
        return POKYNY["us"]
    return POKYNY["obecny"]


def schema(tax):
    return {
        "type": "OBJECT",
        "properties": {
            "heslo": {"type": "STRING"},
            "shrnuti": {"type": "STRING"},
            "oblasti": {"type": "ARRAY", "items": {"type": "STRING", "enum": tax.ids}},
            "procesni": {"type": "BOOLEAN"},
            "vysledek": {"type": "STRING", "enum": list(vysledky.KODY)},
        },
        "required": ["heslo", "shrnuti", "oblasti", "procesni", "vysledek"],
        "propertyOrdering": ["heslo", "shrnuti", "oblasti", "procesni", "vysledek"],
    }


def hlavicka(z):
    """Úvod dotazu: kdo, co, kdy a úřední údaje jako vodítko."""
    radky = [f"Soud: {NAZVY_SOUDU.get(z['soud'], z['soud'])}"]
    for popis, klic in (("Druh", "druh"), ("Spisová značka", "spz"), ("Název věci", "nazev"),
                        ("Datum rozhodnutí", "datum")):
        if z.get(klic):
            radky.append(f"{popis}: {z[klic]}")
    napoveda = [f"{k}: {v}" for k, v in (z.get("meta") or {}).items()
                if v and k not in ("soudce",) and isinstance(v, (str, int))]
    napoveda += [f"{k}: {', '.join(v)}" for k, v in (z.get("meta") or {}).items()
                 if isinstance(v, list) and v]
    if napoveda:
        radky.append("Úřední údaje (vodítko, nemusí být úplné): " + " | ".join(napoveda))
    radky.append(f"Pokyn ke shrnutí: {pokyn(z)}")
    return "\n".join(radky)


def dotaz(z, obsah):
    """Části dotazu: celý text, nebo PDF, když text není."""
    uvod = hlavicka(z)
    text = (obsah.get("text") or "").strip()
    if text:
        return [{"text": uvod + "\n\n--- TEXT ---\n" + text}]
    return [{"inline_data": {"mime_type": "application/pdf",
                             "data": base64.b64encode(obsah["pdf"]).decode("ascii")}},
            {"text": uvod + "\n\nText rozhodnutí je v přiloženém PDF."}]


def _json_z(raw):
    """Objekt JSON z odpovědi (i obalené ```json … ```); None, když tam není."""
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _ano(v):
    if isinstance(v, bool):
        return v
    s = str(v or "").strip().lower()
    if s.startswith(("ano", "yes", "true")):
        return True
    if s.startswith(("ne", "no", "false")):
        return False
    return None


def _cist(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().strip("*").strip()


def parse(raw, tax):
    """Odpověď modelu -> {heslo, shrnuti, oblasti, procesni, vysledek}; None,
    když se nedá použít (chybí shrnutí nebo je podezřele krátké). Výsledek je
    kód z vysledky.KODY, nebo None, když ho model neuvedl."""
    raw = re.sub(r"\*\*|__", "", raw or "")
    data = _json_z(raw)
    if data is not None:
        heslo, shrnuti = data.get("heslo"), data.get("shrnuti") or data.get("shrnutí")
        oblasti, procesni = data.get("oblasti"), data.get("procesni", data.get("procesní"))
        vysledek = data.get("vysledek", data.get("výsledek"))
        if isinstance(oblasti, str):
            oblasti = re.split(r"[,;\n]+", oblasti)
    else:
        def cast(znacka, dalsi):
            m = re.search(rf"{znacka}\s*:\s*(.*?)\s*(?=(?:{dalsi})\s*:|$)", raw, re.I | re.S)
            return m.group(1) if m else ""
        vse = r"HESLO|SHRNUT[IÍ]|OBLASTI|PROCESN[IÍ]|V[YÝ]SLEDEK"
        heslo = cast("HESLO", vse)
        shrnuti = cast(r"SHRNUT[IÍ]", vse)
        oblasti = re.split(r"[,;\n]+", cast("OBLASTI", vse))
        procesni = cast(r"PROCESN[IÍ]", vse)
        vysledek = cast(r"V[YÝ]SLEDEK", vse)
    shrnuti = fc.bez_pravni_formy(_cist(shrnuti))
    if len(shrnuti) < MIN_SHRNUTI:
        return None
    return {
        "heslo": fc.bez_pravni_formy(_cist(heslo)).rstrip("."),
        "shrnuti": shrnuti,
        "oblasti": tax.normalizuj([_cist(o) for o in oblasti or [] if _cist(o)]),
        "procesni": _ano(procesni),
        "vysledek": vysledky.normalizuj(_cist(vysledek)),
    }


# Když AI nevrátí žádnou oblast ze seznamu a nepomohou ani úřední údaje:
# oblast, kam věci od toho soudu patří nejčastěji (u NS podle rejstříku –
# T… je trestní).
NAHRADNI_OBLAST = {"nss": "spravni", "us": "ustavni", "sdeu": "spravni"}


def nahradni_oblasti(z):
    if z.get("oblasti_meta"):
        return list(z["oblasti_meta"])
    if z["soud"] == "ns":
        return ["trestni" if (z.get("rejstrik") or "").startswith("T") else "civilni_proces"]
    return [NAHRADNI_OBLAST.get(z["soud"], "spravni")]


def analyzuj(z, obsah, tax):
    """Rozbor jednoho rozhodnutí. Vrací (výsledek, model) nebo (None, '')."""
    raw, model = fc.ai_volani(dotaz(z, obsah), system=SYSTEM.format(oblasti=tax.do_promptu()),
                              schema=schema(tax), max_tokens=4096,
                              timeout=fc.GEMINI_PDF_TIMEOUT if not obsah.get("text")
                              else fc.GEMINI_TEXT_TIMEOUT)
    if not raw:
        return None, ""
    vysledek = parse(raw, tax)
    if vysledek is None:
        print(f"    [diag] {z['id']}: odpověď AI se nedala použít: {raw[:120]!r}")
        return None, ""
    if not vysledek["oblasti"]:
        vysledek["oblasti"] = nahradni_oblasti(z)
    if vysledek["procesni"] is None:
        vysledek["procesni"] = bool(z.get("procesni_meta"))
    return vysledek, model


KLASIFIKACE_SYSTEM = (
    "Jsi asistent českého advokáta. U každého rozhodnutí níže máš heslo "
    "a shrnutí. Zařaď každé do jedné až tří oblastí ze seznamu (podle věcné "
    "podstaty, první je hlavní) a urči, zda je čistě procesní (bez věcného "
    "právního závěru). Odpověz jen JSON polem objektů {{\"id\": …, \"oblasti\": "
    "[…], \"procesni\": true/false}} v pořadí zadání.\n\n"
    "Seznam oblastí (id – název: co sem patří):\n{oblasti}"
)


def klasifikuj_davku(zaznamy, tax):
    """Doplní oblasti a příznak procesní k hotovým shrnutím (migrace starých
    shrnutí) – jedno volání na dávku. Vrací {id: (oblasti, procesni)}."""
    text = "\n\n".join(
        f"id: {z['id']}\nsp. zn.: {z.get('spz', '')}\nheslo: {(z.get('ai') or {}).get('heslo', '')}\n"
        f"shrnutí: {(z.get('ai') or {}).get('shrnuti', '')}"
        + (f"\núřední heslo: {z['meta'].get('heslo_ns')}" if (z.get('meta') or {}).get('heslo_ns') else "")
        for z in zaznamy)
    raw, _ = fc.ai_volani([{"text": text}],
                          system=KLASIFIKACE_SYSTEM.format(oblasti=tax.do_promptu()),
                          max_tokens=8192)
    m = re.search(r"\[.*\]", re.sub(r"\*\*|__", "", raw or ""), re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return {}
    out = {}
    for polozka in data if isinstance(data, list) else []:
        if isinstance(polozka, dict) and polozka.get("id"):
            out[str(polozka["id"])] = (tax.normalizuj(polozka.get("oblasti") or []),
                                       _ano(polozka.get("procesni")))
    return out
