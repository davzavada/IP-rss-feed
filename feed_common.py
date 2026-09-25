#!/usr/bin/env python3
"""Sdílené utility pro RSS scrapery: sledování prvního výskytu položek a
volitelné AI shrnutí přes Gemini API.

Sledování prvního výskytu: každý feed si drží JSON {guid: ISO datum prvního
výskytu}. Podle něj:
  - ponecháme jen položky s prvním výskytem do `weeks` týdnů zpět,
  - označíme položky, které přibyly v posledních 24 hodinách
    (item["is_new"] = True).

Tím je doba zobrazení stabilní (nezávisí na tom, když zdroj přepíše datum)
a u všech feedů jednotná.

AI: jednotný klient Gemini API na free tieru – Flash-Lite, a když nemůže,
nejnovější Gemma; při vyčerpaném limitu nebo výpadku přejde na další model.
Sdílí ho všechny scrapery i přehled. Cache shrnutí mají všechny feedy
stejnou (summarize_with_cache).
"""

import atexit
import base64
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

# Weby soudů i vydavatelů občas holý requests odmítnou – hlásíme se jako
# běžný prohlížeč. Sdílí to každý scraper, ať se řetězec neopisuje.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Jak dlouho držet záznam o prvním výskytu. Musí být delší než nejdelší
# zobrazované okno (CJEU 8 týdnů), jinak by se položka po vypadnutí ze stavu
# označila podruhé jako nová. Cache shrnutí se prořezává podle téhož stavu.
SEEN_PRUNE_DAYS = 120

# „Nové" = přibylo v posledních 24 hodinách. Kalendářní den se k tomu nehodí:
# ranní okno běhů (23:00–7:00 Praha) jde přes půlnoc, takže by položky
# nalezené před půlnocí přišly o příznak dřív, než si je ráno někdo přečte.
NEW_WINDOW = timedelta(hours=24)


def is_new(first_seen, now=None):
    """True, když položka přibyla v posledních NEW_WINDOW hodinách."""
    return first_seen >= (now or datetime.now(timezone.utc)) - NEW_WINDOW


def save_seen(state_file, seen, prune_days=SEEN_PRUNE_DAYS):
    """Uloží stav, vyhodí záznamy starší než prune_days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=prune_days)
    pruned = {}
    for guid, ts in seen.items():
        try:
            if datetime.fromisoformat(ts) >= cutoff:
                pruned[guid] = ts
        except (ValueError, TypeError):
            continue
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(pruned, f, ensure_ascii=False, indent=2)


def filter_by_first_seen(items, guid_of, state_file, weeks=2):
    """Ponechá jen položky s prvním výskytem do `weeks` týdnů zpět.

    Každé ponechané položce nastaví item["is_new"] = True, pokud přibyla
    v posledních 24 hodinách. Stav prvního výskytu zároveň uloží.

    items    – seznam dict položek
    guid_of  – funkce item -> stabilní identifikátor (str)
    """
    seen = load_json(state_file)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(weeks=weeks)

    kept = []
    for item in items:
        guid = guid_of(item)
        if not guid:
            continue
        if guid not in seen:
            seen[guid] = now.isoformat()
        first_seen = datetime.fromisoformat(seen[guid])
        if first_seen >= cutoff:
            item["is_new"] = is_new(first_seen, now)
            # Zdroje bez data vydání (weby, které ho neuvádějí) si tímhle
            # můžou doplnit aspoň datum, kdy položka přibyla.
            item["first_seen"] = first_seen
            kept.append(item)

    save_seen(state_file, seen)
    return kept


# --- Jednoduchá JSON cache (např. {guid: {"summary": ..., "tag": ...}}) ---

def load_json(path, default=None):
    """Načte JSON soubor, vrátí `default` (nebo {}), když neexistuje."""
    if not os.path.exists(path):
        return {} if default is None else default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    """Uloží data jako čitelný JSON (UTF-8)."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def prune_meta(meta, state_file):
    """Ponechá v meta cache jen záznamy, jejichž klíč je i ve stavu prvního
    výskytu. Stav se prořezává po SEEN_PRUNE_DAYS dnech, takže cache roste
    s ním a ne donekonečna.

    Když je stav prázdný (čerstvý reset sledování), cache raději nechá být.
    """
    seen = load_json(state_file)
    if not seen:
        return meta
    return {k: v for k, v in meta.items() if k in seen}


def prune_meta_file(meta_file, state_file):
    """Prořízne cache v souboru podle stavu prvního výskytu (viz prune_meta)."""
    save_json(meta_file, prune_meta(load_json(meta_file), state_file))


def summarize_with_cache(items, meta_file, key_of, summarize, needs_call=None,
                         fields=("summary", "tag")):
    """Doplní položkám AI shrnutí z cache; co v ní není, nechá dopočítat.

    Jeden průchod pro všechny feedy: cache je JSON {klíč: {"summary": …,
    "tag": …, …}}, položka dostane pole `fields` (hodnota, kterou už má,
    má přednost před cache).

    key_of      – item -> klíč do cache; prázdný klíč = položku přeskočit
    summarize   – (item, cached) -> dict polí k uložení do cache, nebo None.
                  Volá se jen se zapnutou AI a jen když je co dělat.
    needs_call  – (item, cached) -> bool; výchozí „chybí shrnutí".
    """
    meta = load_json(meta_file)
    ai = gemini_enabled()
    if needs_call is None:
        def needs_call(item, cached):
            return not cached.get("summary")
    calls = done = 0
    for item in items:
        key = key_of(item)
        cached = meta.get(key, {}) if key else {}
        if key and ai and needs_call(item, cached):
            calls += 1
            got = summarize(item, cached) or {}
            if got:
                cached = dict(cached, **got)
                meta[key] = cached
                if got.get("summary"):
                    done += 1
        for field in fields:
            item[field] = item.get(field) or cached.get(field, "")
    save_json(meta_file, meta)
    if calls:
        print(f"  AI: {done} shrnutí z {calls} pokusů")
    return items


# --- AI přes Gemini API na free tieru ---
# Klíč je z Google AI Studia a projekt nemá zapnutý billing, takže všechno
# běží zdarma, ale s denními a minutovými limity zvlášť pro každý model.
# Pro a Flash se nepoužívají: Pro má na free tieru kvótu vyčerpanou hned
# a Flash bývá přetížený (503). Klient proto začíná Flash-Lite (rychlý, bere
# celé texty) a pak zkusí nejnovější Gemmu. Alias „-latest" posouvá Google
# sám na nejnovější verzi, takže nový model se použije bez zásahu do kódu.
# Model, který na free tieru není (429 s nulovou kvótou) nebo neexistuje
# (404), se vyřadí; model s vyčerpaným denním limitem se přeskočí do konce
# běhu. Pořadí jde vnutit proměnnou GEMINI_MODELS (názvy oddělené čárkou).
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_VYCHOZI_MODELY = ("gemini-flash-lite-latest",)
# Poslední záloha, když se nejnovější Gemma nedá zjistit ze seznamu modelů.
GEMMA_ZALOZNI = "gemma-4-31b-it"
# Rozestup mezi voláními téhož modelu (s). Limity za minutu Google u free
# tieru nezveřejňuje – tohle jsou opatrné hodnoty; když model přesto vrátí
# 429 za minutu, počká se, kolik si řekne (RetryInfo).
GEMINI_ROZESTUP = {"pro": 12.0, "flash-lite": 4.0, "flash": 6.0, "gemma": 2.5}
GEMINI_MIN_INTERVAL = 5.0   # základ backoffu při výpadku
# Pokusy na jeden model při přetížení a timeoutu. Méně než dřív s jedinou
# Gemmou: přetížený model (503) je častý a další v pořadí odpoví dřív.
GEMINI_MAX_RETRIES = 3
GEMINI_MAX_CEKANI = 90      # nejdéle tolik se čeká na minutový limit
# Přetížení u Googlu bývá na minuty. Model, který selže na třech položkách
# po sobě, dostane pauzu; když mají pauzu všechny, počká se na první z nich.
# Do konce běhu pryč je model až po třetí pauze.
GEMINI_PAUZA_S = 600
GEMINI_MAX_PAUZ = 3
# Časové limity jednoho volání. Posíláme celé texty a nejlepší modely nad
# nimi přemýšlejí – běh smí být delší, shrnutí je cennější než rychlost.
GEMINI_TEXT_TIMEOUT = 180
GEMINI_PDF_TIMEOUT = 300
# Opakovat na témže modelu má smysl jen u dočasných potíží. 429 se řeší zvlášť
# podle toho, jestli jde o limit za minutu, za den, nebo o model mimo free tier.
GEMINI_RETRY_STATUSES = (408, 500, 502, 503, 504)
# Bezpečnostní filtry na minimum – trestní rozhodnutí by jinak padala.
GEMINI_BEZPECNOST = [
    {"category": c, "threshold": "BLOCK_NONE"}
    for c in ("HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
              "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")
]

# Prompt pro rozhodnutí NS ČR.
JUDIKATURA_PROMPT = (
    "Toto je rozhodnutí Nejvyššího soudu ČR. Odpověz česky přesně ve dvou "
    "částech, bez úvodních frází a bez dalšího textu:\n"
    "HESLO: výstižné právní téma sporu o 1–3 slovech (např. Nekalá soutěž, "
    "Smluvní pokuta, Autorské právo, Rozsudek pro uznání, Promlčení).\n"
    "SHRNUTÍ: nejvýše tři věty. V první větě stručně kdo se s kým soudil "
    "(uveď jména stran, ale bez právní formy, tj. bez s.r.o., a.s., spol. "
    "apod.) a o co šlo. Pak uveď, jakou právní otázku soud řešil a jak ji "
    "vyřešil – konkrétní právní závěr soudu (např. „Podle soudu se právo "
    "na informace podle § 40 autorského zákona nepromlčuje.“). Drž se "
    "stručnosti."
)

# Prompt pro odborný právní článek (z názvu a anotace).
JOURNAL_ARTICLE_PROMPT = (
    "Toto je odborný právní článek (název a anotace). Odpověz česky přesně "
    "ve dvou částech, bez úvodních frází a bez dalšího textu:\n"
    "HESLO: výstižné téma článku o 1–3 slovech.\n"
    "SHRNUTÍ: nejvýše tři věty – o čem článek je a k jakým hlavním závěrům "
    "nebo zjištěním dochází. Drž se stručnosti a nic si nevymýšlej."
)

# Prompt pro soudní rozhodnutí otištěné v časopise. IIC k rozhodnutím
# zahraničních soudů tiskne i úřední právní věty, takže je z čeho shrnovat.
JOURNAL_DECISION_PROMPT = (
    "Toto je soudní rozhodnutí otištěné v odborném právním časopise – "
    "zpravidla rozhodnutí zahraničního soudu, často i s úředními právními "
    "větami. Odpověz česky přesně ve dvou částech, bez úvodních frází "
    "a bez dalšího textu:\n"
    "HESLO: výstižné právní téma o 1–3 slovech (např. FRAND, Ochranná "
    "známka, Patent, Nekalá soutěž).\n"
    "SHRNUTÍ: nejvýše tři věty. V první uveď, který soud a v jaké věci "
    "rozhodl. Dál jakou právní otázku řešil a s jakým závěrem. Drž se "
    "stručnosti a nic si nevymýšlej."
)

# Prompt pro celé číslo právního časopisu (z PDF).
JOURNAL_ISSUE_PROMPT = (
    "Toto je celé číslo odborného právního časopisu. Odpověz česky přesně "
    "ve dvou částech, bez úvodních frází a bez dalšího textu:\n"
    "HESLO: hlavní oblast tohoto čísla o 1–3 slovech.\n"
    "SHRNUTÍ: nejvýše tři věty – jaká hlavní témata a příspěvky toto číslo "
    "obsahuje. Drž se stručnosti a nic si nevymýšlej."
)

# Prompt pro dvoutýdenní přehled – shrnutí shrnutí ze všech feedů dohromady.
# Vstupem je číslovaný seznam položek (zdroj, název, heslo, shrnutí), výstupem
# krátký přehled po tématech. Čísla položek v ZDROJE se překládají zpět na
# odkazy (viz digest.py).
DIGEST_PROMPT = (
    "Jsi asistent českého advokáta se specializací na právo duševního "
    "vlastnictví a IT. Níže je číslovaný seznam položek za poslední dva "
    "týdny: rozhodnutí Nejvyššího soudu, Nejvyššího správního soudu "
    "a Ústavního soudu, rozhodnutí, stanoviska a předběžné otázky Soudního "
    "dvora EU, která spadají do duševního vlastnictví nebo IT, a články "
    "z právních časopisů.\n\n"
    "Napiš česky přehled toho, co se za ty dva týdny stalo v duševním "
    "vlastnictví a IT – a jen v nich: autorské právo, ochranné známky, "
    "patenty a užitné vzory, průmyslové vzory, nekalá soutěž, know-how "
    "a obchodní tajemství, licence, doménová jména, ochrana osobních údajů, "
    "umělá inteligence, platformy a digitální služby. Co do těchto oblastí "
    "nepatří, vynech, i když je to jinak zajímavé (typicky obecné články "
    "z časopisů, které nejsou o IP ani IT). Vynech i to, co je jen obecné, "
    "pro praxi nepodstatné nebo se týká právních řádů mimo ČR a EU (ledaže "
    "jde o věc, která je zajímavá i odsud). Nic si nevymýšlej, drž se toho, "
    "co je ve shrnutích; u čeho si nejsi jistý, raději vynech.\n\n"
    "Odpověz přesně v tomto formátu, bez úvodních frází a bez dalšího "
    "textu:\n"
    "PŘEHLED: dvě až tři věty o tom, čím bylo období jako celek zajímavé.\n"
    "Pak dva až pět bloků seřazených od nejdůležitějšího, každý přesně "
    "takto:\n"
    "TÉMA: nadpis o 2–5 slovech\n"
    "TEXT: dvě až čtyři věty – co se stalo a co to znamená pro praxi.\n"
    "ZDROJE: čísla položek z uvedeného seznamu oddělená čárkou (např. 3, 7)"
)


def gemini_enabled():
    """True, když je nastaven API klíč a není zapnuté SKIP_GEMINI."""
    if not GEMINI_API_KEY:
        return False
    return os.environ.get("SKIP_GEMINI", "").lower() not in ("1", "true", "yes")


# Právní formy za jménem (s.r.o., a. s., GmbH…). Prompty je zakazují, modely
# je přesto občas napíšou – odstraní se tady. Krátké velké zkratky (AG, SA…)
# jen hned za slovem s velkým písmenem nebo číslicí (za jménem firmy), ať
# nezmizí „GA“ ani „se“. Čárka před formou jde pryč s ní, a když byla forma
# v čárkách („Spolek, z. s., a obec“), i ta za ní.
_FORMY = (r"spol\.\s?s\s?r\.\s?o\.", r"s\.\s?r\.\s?o\.", r"a\.\s?s\.", r"v\.\s?o\.\s?s\.", r"k\.\s?s\.",
          r"z\.\s?s\.", r"z\.\s?ú\.", r"o\.\s?p\.\s?s\.", r"s\.\s?p\.", r"sp\.\s?z\s?o\.\s?o\.",
          r"S\.\s?p\.\s?A\.", r"S\.\s?A\.", r"S\.\s?à\s?r\.\s?l\.", r"S\.\s?r\.\s?l\.", r"B\.\s?V\.",
          r"N\.\s?V\.", r"d\.\s?o\.\s?o\.", r"Kft\.", r"Zrt\.", r"GmbH(?:\s?&\s?Co\.\s?KG)?", r"Ltd\.?",
          r"LLC", r"Inc\.?", r"plc", r"PLC", r"SARL")
_ZKRATKY = (r"AG", r"SA", r"SE", r"AB", r"BV", r"NV", r"KG", r"SAS", r"SpA", r"SRL", r"Oy", r"A/S", r"ApS")
_KONEC_FORMY = r"(?=$|[\s,.;:!?)\]“”\"'])"
_FORMA_RE = re.compile(r"(?:(?P<carka>,)\s*|\s+)(?P<forma>" + "|".join(_FORMY) + r")(?(carka),?)"
                       + _KONEC_FORMY)
_ZKRATKA_RE = re.compile(r"(?P<jmeno>\b[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ0-9][\w&.'\-]*)(?:(?P<carka>,)\s*|\s+)"
                         r"(?P<forma>" + "|".join(_ZKRATKY) + r")(?(carka),?)" + _KONEC_FORMY)
_NOVA_VETA_RE = re.compile(r"\s*$|\s+[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]")


def bez_pravni_formy(text):
    """„Alfa s.r.o. se soudila“ -> „Alfa se soudila“. Když tečka formy byla
    zároveň koncem věty, zůstane."""
    if not text:
        return text or ""

    def pryc(m):
        konec_vety = m.group(0).endswith(".") and _NOVA_VETA_RE.match(m.string, m.end())
        return "." if konec_vety else ""

    text = _FORMA_RE.sub(pryc, text)
    return _ZKRATKA_RE.sub(lambda m: m.group("jmeno"), text)


def parse_ai_response(raw):
    """Rozparsuje odpověď modelu ve tvaru 'HESLO: ...' + 'SHRNUTÍ: ...'.

    Vrací (shrnutí, heslo). Když značky chybí, bere celý text jako shrnutí.
    Modely Gemini rády píšou značky tučně (**HESLO:**) – markdown se zahodí.
    """
    def clean(s):
        return re.sub(r"\s+", " ", s).strip()

    raw = re.sub(r"\*\*|__", "", raw or "")
    heslo = ""
    mh = re.search(r"HESLO:\s*(.*?)\s*(?=SHRNUT[IÍ]:|$)", raw, re.IGNORECASE | re.DOTALL)
    if mh:
        heslo = clean(mh.group(1)).rstrip(".")
    ms = re.search(r"SHRNUT[IÍ]:\s*(.*)", raw, re.IGNORECASE | re.DOTALL)
    if ms:
        summary = ms.group(1)
    elif mh:
        summary = raw[mh.end():]
    else:
        summary = raw
    return clean(summary), heslo


# --- Klient: výběr modelu, limity, zálohy ---

_zamek = threading.Lock()   # chrání sdílený stav níže
_poradi = None              # seřazené modely pro tento proces (líně z models.list)
_modely = {}                # model -> stav (rozestup, vyřazení, vypnuté volby…)
_spotreba = {}              # model -> {"volani", "vstup", "vystup"}
_klic_zamitnut = False      # klíč neplatný nebo zablokovaný: dál nezkoušet
_posledni_pretizeni = False  # poslední neúspěch ai_volani byl kvůli přetížení


def _rodina(model):
    """Rodina modelu kvůli rozestupům a podporovaným volbám."""
    m = model.lower()
    if m.startswith("gemma"):
        return "gemma"
    if "flash-lite" in m:
        return "flash-lite"
    if "flash" in m:
        return "flash"
    return "pro" if "pro" in m else "flash"


def _stav(model):
    with _zamek:
        st = _modely.get(model)
        if st is None:
            st = _modely[model] = {
                "rozestup": GEMINI_ROZESTUP.get(_rodina(model), GEMINI_MIN_INTERVAL),
                "dalsi": 0.0,            # kdy nejdřív smí jít další volání
                "vyrazen": "",           # důvod, proč se model do konce běhu nezkouší
                "pauza_do": 0.0,         # do kdy (time.monotonic) má přetížený model pauzu
                "pauz": 0,               # kolik pauz už měl
                "vypnuto": set(),        # volby, které model odmítl (system, mysleni…)
                "selhani": 0,            # položky po sobě, na kterých model selhal
                "zamek": threading.Lock(),
            }
        return st


def _seznam_modelu():
    """Názvy modelů, které klíč smí volat přes generateContent; None, když
    se seznam nepodaří stáhnout (pak se jede podle výchozího pořadí)."""
    try:
        r = requests.get(f"{GEMINI_API}/models", params={"pageSize": 1000},
                         headers={"x-goog-api-key": GEMINI_API_KEY}, timeout=30)
        if r.status_code != 200:
            print(f"    AI: seznam modelů nedostupný ({r.status_code})")
            return None
        return {m["name"].split("/", 1)[-1] for m in r.json().get("models", [])
                if "generateContent" in m.get("supportedGenerationMethods", [])}
    except Exception as e:
        print(f"    AI: seznam modelů nedostupný ({e})")
        return None


def _nejnovejsi_gemma(dostupne):
    """Nejnovější a největší „hustá" Gemma (gemma-4-31b-it před gemma-4-26b-a4b-it
    i před gemma-3-27b-it); None, když žádná v seznamu není."""
    kandidati = []
    for nazev in dostupne or ():
        m = re.fullmatch(r"gemma-(\d+(?:\.\d+)?)-(\d+)b-it", nazev)
        if m:
            kandidati.append((float(m.group(1)), int(m.group(2)), nazev))
    return max(kandidati)[2] if kandidati else None


def gemini_modely():
    """Pořadí modelů pro tento proces: vnucené GEMINI_MODELS, jinak výchozí
    aliasy, které klíč zná, a nakonec nejnovější Gemma."""
    global _poradi
    if _poradi is not None:
        return _poradi
    vnucene = [m.strip() for m in os.environ.get("GEMINI_MODELS", "").split(",") if m.strip()]
    if vnucene:
        poradi = vnucene
    else:
        dostupne = _seznam_modelu()
        poradi = [m for m in GEMINI_VYCHOZI_MODELY if dostupne is None or m in dostupne]
        poradi.append(_nejnovejsi_gemma(dostupne) or GEMMA_ZALOZNI)
    with _zamek:
        if _poradi is None:
            _poradi = poradi
            print(f"    AI: pořadí modelů {', '.join(_poradi)}")
    return _poradi


def _pockej_na_model(model):
    """Rozestup mezi voláními téhož modelu; bezpečné i pro víc vláken."""
    st = _stav(model)
    with st["zamek"]:
        ted = time.monotonic()
        if st["dalsi"] > ted:
            time.sleep(st["dalsi"] - ted)
        st["dalsi"] = time.monotonic() + st["rozestup"]


def _telo(model, parts, system, schema, max_tokens):
    """JSON dotazu pro daný model, bez voleb, které model dřív odmítl."""
    st = _stav(model)
    gemma = _rodina(model) == "gemma"
    vypnuto = st["vypnuto"]
    if system and (gemma or "system" in vypnuto):
        # Gemma systémové instrukce nebere – předřadí se textu dotazu.
        parts = [{"text": system}] + list(parts)
        system = None
    config = {"temperature": 0.2, "maxOutputTokens": max_tokens}
    telo = {"contents": [{"role": "user", "parts": list(parts)}], "generationConfig": config}
    if system:
        telo["systemInstruction"] = {"parts": [{"text": system}]}
    if schema and not gemma and "json" not in vypnuto:
        config["responseMimeType"] = "application/json"
        config["responseSchema"] = schema
    if not gemma and "mysleni" not in vypnuto:
        # Na shrnutí stačí málo přemýšlení a je výrazně rychlejší.
        config["thinkingConfig"] = {"thinkingLevel": "low"}
    if "bezpecnost" not in vypnuto:
        telo["safetySettings"] = GEMINI_BEZPECNOST
    return telo


# Podle slova ve zprávě u 400 se pozná, kterou volbu model nebere.
_VOLBY_Z_CHYBY = (
    ("mysleni", ("thinking",)),
    ("json", ("responseschema", "response_schema", "responsemimetype",
              "response_mime_type", "json mode")),
    ("system", ("systeminstruction", "system_instruction", "developer instruction")),
    ("bezpecnost", ("safety",)),
)


def _kvota_z_chyby(chyba):
    """Z těla 429 vytáhne (denní limit?, nulová kvóta?, čekání v s)."""
    denni = nulova = False
    cekani = None
    for d in chyba.get("details") or []:
        typ = d.get("@type", "")
        if typ.endswith("QuotaFailure"):
            for v in d.get("violations") or []:
                jmeno = f"{v.get('quotaId', '')} {v.get('quotaMetric', '')}".lower()
                if "perday" in jmeno or "per_day" in jmeno:
                    denni = True
                if str(v.get("quotaValue", "")).strip() == "0":
                    nulova = True
        elif typ.endswith("RetryInfo"):
            m = re.match(r"([\d.]+)s", str(d.get("retryDelay", "")))
            if m:
                cekani = float(m.group(1))
    return denni, nulova, cekani


def _zapis_spotrebu(model, data):
    u = data.get("usageMetadata") or {}
    with _zamek:
        sp = _spotreba.setdefault(model, {"volani": 0, "vstup": 0, "vystup": 0})
        sp["volani"] += 1
        sp["vstup"] += int(u.get("promptTokenCount") or 0)
        sp["vystup"] += int(u.get("candidatesTokenCount") or 0) + int(u.get("thoughtsTokenCount") or 0)


def ai_spotreba():
    """Kopie spotřeby v tomto procesu: {model: {volani, vstup, vystup}}."""
    with _zamek:
        return {m: dict(v) for m, v in _spotreba.items()}


@atexit.register
def _vypis_spotrebu():
    def cislo(n):
        return f"{n:,}".replace(",", " ")
    for model, sp in ai_spotreba().items():
        print(f"AI spotřeba {model}: {sp['volani']} volání, "
              f"{cislo(sp['vstup'])} tokenů vstupu, {cislo(sp['vystup'])} výstupu")


def _vyrad(model, duvod):
    st = _stav(model)
    if not st["vyrazen"]:
        st["vyrazen"] = duvod
        print(f"    AI: {model} – {duvod}, dál bez něj")


def _klic_neplatny(chyba, zprava):
    """400 kvůli klíči (smazaný, přepsaný, s překlepem): Gemini ho hlásí jako
    INVALID_ARGUMENT s důvodem API_KEY_INVALID. S takovým klíčem nepůjde nic."""
    for detail in chyba.get("details") or []:
        if isinstance(detail, dict) and detail.get("reason") == "API_KEY_INVALID":
            return True
    return "api key not valid" in zprava.lower()


def _zkus_model(model, telo_fn, timeout):
    """Jeden model, s opakováním při dočasných potížích.

    Vrací (text, verze) při úspěchu; jinak ('', důvod), kde důvod říká, proč
    má položka zkusit další model („dalsi"), nebo že nemá smysl zkoušet nic
    („konec")."""
    global _klic_zamitnut
    st = _stav(model)
    for pokus in range(GEMINI_MAX_RETRIES):
        _pockej_na_model(model)
        try:
            r = requests.post(
                f"{GEMINI_API}/models/{model}:generateContent",
                headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
                json=telo_fn(), timeout=timeout,
            )
        except Exception as e:
            if pokus + 1 < GEMINI_MAX_RETRIES:
                cekej = GEMINI_MIN_INTERVAL * (2 ** pokus)
                print(f"    AI {model}: {type(e).__name__} – čekám {cekej:.0f}s "
                      f"(pokus {pokus + 1}/{GEMINI_MAX_RETRIES})")
                time.sleep(cekej)
            continue
        if r.status_code == 200:
            data = r.json()
            kandidati = data.get("candidates") or []
            if not kandidati:
                duvod = (data.get("promptFeedback") or {}).get("blockReason", "bez odpovědi")
                print(f"    AI {model}: zablokováno ({duvod}), zkusím další model")
                return "", "dalsi"
            cand = kandidati[0]
            text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", [])
                           if not p.get("thought")).strip()
            konec = cand.get("finishReason")
            _zapis_spotrebu(model, data)
            if konec == "MAX_TOKENS" or not text:
                print(f"    AI {model}: bez celé odpovědi ({konec}), zkusím další model")
                return "", "dalsi"
            st["selhani"] = 0
            return text, data.get("modelVersion") or model
        chyba = {}
        try:
            chyba = r.json().get("error") or {}
        except ValueError:
            pass
        zprava = re.sub(r"\s+", " ", str(chyba.get("message") or r.text))[:200]
        if r.status_code == 429:
            denni, nulova, cekani = _kvota_z_chyby(chyba)
            if nulova:
                _vyrad(model, "na free tieru není")
                return "", "dalsi"
            if denni:
                _vyrad(model, "denní limit vyčerpán")
                return "", "dalsi"
            cekej = min(cekani or GEMINI_MIN_INTERVAL * (2 ** pokus), GEMINI_MAX_CEKANI)
            print(f"    AI {model}: limit za minutu – čekám {cekej:.0f}s")
            time.sleep(cekej)
            continue
        if r.status_code == 400 and _klic_neplatny(chyba, zprava):
            _klic_zamitnut = True
            print(f"    AI: neplatný klíč ({zprava}) – AI v tomto běhu končí")
            return "", "konec"
        if r.status_code == 400:
            nizko = zprava.lower()
            for volba, slova in _VOLBY_Z_CHYBY:
                if volba not in st["vypnuto"] and any(w in nizko for w in slova):
                    st["vypnuto"].add(volba)
                    print(f"    AI {model}: nebere volbu „{volba}\", posílám bez ní")
                    break
            else:
                # Třeba příliš dlouhý vstup – jiný model ho může vzít.
                print(f"    AI {model}: 400 {zprava}")
                return "", "dalsi"
            continue
        if r.status_code == 404:
            _vyrad(model, "neexistuje")
            return "", "dalsi"
        if r.status_code in (401, 403):
            _klic_zamitnut = True
            print(f"    AI: klíč odmítnut ({r.status_code}: {zprava}) – AI v tomto běhu končí")
            return "", "konec"
        if r.status_code in GEMINI_RETRY_STATUSES:
            if pokus + 1 < GEMINI_MAX_RETRIES:
                cekej = GEMINI_MIN_INTERVAL * (2 ** pokus)
                print(f"    AI {model}: {r.status_code} – čekám {cekej:.0f}s "
                      f"(pokus {pokus + 1}/{GEMINI_MAX_RETRIES})")
                time.sleep(cekej)
            continue
        print(f"    AI {model}: {r.status_code} {zprava}")
        return "", "dalsi"
    # Pokusy došly na dočasných chybách (5xx, timeout, minutový limit). Model
    # zůstává ve hře, ale když selže na třech položkách po sobě, je nejspíš
    # přetížený – dostane pauzu, po třetí pauze je do konce běhu pryč.
    st["selhani"] += 1
    if st["selhani"] >= 3:
        st["selhani"] = 0
        st["pauz"] += 1
        if st["pauz"] >= GEMINI_MAX_PAUZ:
            _vyrad(model, "opakovaně nedostupný")
        else:
            st["pauza_do"] = time.monotonic() + GEMINI_PAUZA_S
            print(f"    AI: {model} – přetížený, pauza {GEMINI_PAUZA_S // 60} min")
    return "", "pretizeni"


def _v_pauze(model):
    return _stav(model)["pauza_do"] > time.monotonic()


def ai_pretizena():
    """Selhalo poslední volání ai_volani jen kvůli přetížení (5xx, limity za
    minutu, pauzy)? Pak za to dotaz nemůže a pokus se mu nemá počítat."""
    return _posledni_pretizeni


def ai_volani(parts, system=None, schema=None, max_tokens=8192, timeout=GEMINI_TEXT_TIMEOUT):
    """Pošle dotaz nejlepšímu dostupnému modelu, při neúspěchu dalšímu.

    parts   – části dotazu (text, inline_data), jak je bere generateContent
    system  – systémová instrukce (Gemmě se předřadí textu)
    schema  – JSON schéma odpovědi pro modely, které ho umějí (jinak text)

    Vrací (odpověď, model); ('', '') při neúspěchu nebo vypnutém AI. Jestli
    neúspěch způsobilo jen přetížení, řekne potom ai_pretizena()."""
    global _posledni_pretizeni
    _posledni_pretizeni = False
    if not gemini_enabled() or _klic_zamitnut:
        return "", ""
    # Za přetížení (pokus se rozhodnutí nepočítá) se bere, když aspoň jeden
    # model selhal jen dočasně – ten ho příště může vzít, i když ho jiný
    # zablokoval (PROHIBITED_CONTENT u trestních věcí) nebo nevzal pro délku.
    # Když nevyšel žádný model, jsou všechny v pauze nebo vyřazené.
    pretizeni, zkouseno = False, False
    for kolo in range(2):
        for model in gemini_modely():
            if _stav(model)["vyrazen"] or _v_pauze(model):
                continue
            zkouseno = True
            text, vysledek = _zkus_model(
                model, lambda: _telo(model, parts, system, schema, max_tokens), timeout)
            if text:
                return text, vysledek
            if vysledek == "konec":
                print("    AI: žádný model nedal odpověď, zkusím příště")
                return "", ""
            if vysledek == "pretizeni":
                pretizeni = True
        pretizeni = pretizeni or not zkouseno
        # Když zbylé modely jen čekají na konec pauzy, počká se na první z nich
        # a zkusí se to ještě jednou.
        pauzy = [_stav(m)["pauza_do"] for m in gemini_modely() if not _stav(m)["vyrazen"]]
        if kolo or not pretizeni or not pauzy or min(pauzy) <= time.monotonic():
            break
        cekej = min(pauzy) - time.monotonic()
        print(f"    AI: všechny modely mají pauzu, čekám {cekej / 60:.0f} min")
        time.sleep(cekej)
    _posledni_pretizeni = pretizeni
    print("    AI: žádný model nedal odpověď, zkusím příště")
    return "", ""


def _gemini_generate(parts, max_tokens=8192, timeout=GEMINI_TEXT_TIMEOUT):
    """Surový text odpovědi (starší rozhraní, beze jména modelu)."""
    return ai_volani(parts, max_tokens=max_tokens, timeout=timeout)[0]


def gemini_summarize_pdf(pdf_bytes, prompt):
    """Pošle PDF + prompt, vrátí (shrnutí, heslo). ('', '') při neúspěchu."""
    if not pdf_bytes:
        return "", ""
    parts = [
        {"inline_data": {
            "mime_type": "application/pdf",
            "data": base64.b64encode(pdf_bytes).decode("ascii"),
        }},
        {"text": prompt},
    ]
    return parse_ai_response(_gemini_generate(parts, timeout=GEMINI_PDF_TIMEOUT))


def gemini_summarize_text(text, prompt):
    """Pošle text + prompt, vrátí (shrnutí, heslo). ('', '') při neúspěchu.

    Text jde celý – modely berou stovky tisíc tokenů a rozhodnutí se má
    shrnovat z celého znění, ne z ořezu."""
    text = (text or "").strip()
    if not text:
        return "", ""
    parts = [{"text": prompt + "\n\n--- TEXT ---\n" + text}]
    return parse_ai_response(_gemini_generate(parts))


def gemini_generate_raw(prompt, text, max_tokens=8192, timeout=300):
    """Pošle prompt + text a vrátí surovou odpověď bez parsování.

    Pro delší výstupy, které nemají tvar HESLO/SHRNUTÍ (dvoutýdenní přehled,
    rozvrh práce, dávky jmen). Vrací '' při neúspěchu nebo vypnutém AI."""
    text = (text or "").strip()
    if not text:
        return ""
    parts = [{"text": prompt + "\n\n--- POLOŽKY ---\n" + text}]
    return _gemini_generate(parts, max_tokens=max_tokens, timeout=timeout).strip()
