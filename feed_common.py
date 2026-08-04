#!/usr/bin/env python3
"""Sdílené utility pro RSS scrapery: sledování prvního výskytu položek a
volitelné AI shrnutí přes Gemma (Gemini API).

Sledování prvního výskytu: každý feed si drží JSON {guid: ISO datum prvního
výskytu}. Podle něj:
  - ponecháme jen položky s prvním výskytem do `weeks` týdnů zpět,
  - označíme položky poprvé viděné dnes (item["is_new"] = True).

Tím je doba zobrazení stabilní (nezávisí na tom, když zdroj přepíše datum)
a u všech feedů jednotná.

AI shrnutí: jednotný klient Gemma 4 31B (štědrý free-tier) s throttlingem
a opakováním při 429/5xx. Sdílí ho hlavní scraper i ostatní feedy.
"""

import base64
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests


def load_seen(state_file):
    """Načte {guid: iso_datum_prvniho_vyskytu}."""
    if not os.path.exists(state_file):
        return {}
    with open(state_file, "r", encoding="utf-8") as f:
        return json.load(f)


def save_seen(state_file, seen, prune_days=90):
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

    Každé ponechané položce nastaví item["is_new"] = True, pokud byla
    poprvé viděna dnes. Stav prvního výskytu zároveň uloží.

    items    – seznam dict položek
    guid_of  – funkce item -> stabilní identifikátor (str)
    """
    seen = load_seen(state_file)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(weeks=weeks)
    today = now.date()

    kept = []
    for item in items:
        guid = guid_of(item)
        if not guid:
            continue
        if guid not in seen:
            seen[guid] = now.isoformat()
        first_seen = datetime.fromisoformat(seen[guid])
        if first_seen >= cutoff:
            item["is_new"] = (first_seen.date() == today)
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
    výskytu. Stav se prořezává po ~90–120 dnech, takže cache roste s ním
    a ne donekonečna.

    Když je stav prázdný (čerstvý reset sledování), cache raději nechá být.
    """
    seen = load_seen(state_file)
    if not seen:
        return meta
    return {k: v for k, v in meta.items() if k in seen}


# --- AI shrnutí přes Gemma (Gemini API) ---
# Gemma 4 31B má štědrý free-tier; throttlujeme na 12 požadavků/min (5 s mezi
# voláními) a opakujeme při 429/500/502/503. Throttle je per-proces – každý
# scraper běží jako vlastní proces, takže si vystačí s vlastním rozestupem.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemma-4-31b-it"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)
GEMINI_MIN_INTERVAL = 5.0   # s mezi voláními (= 12 req/min)
GEMINI_MAX_RETRIES = 3      # opakování při 429/500/502/503
_gemini_last_call = 0.0

# Prompt pro rozhodnutí NS ČR – sdílí ho hlavní feed i feed nepřímého účinku.
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

# Prompt pro feed nepřímého účinku – cílem je popsat, JAK soud nepřímý
# účinek unijního práva v rozhodnutí použil (ne obecné shrnutí sporu).
NEPRIMY_PROMPT = (
    "Toto je rozhodnutí Nejvyššího soudu ČR, které pracuje s nepřímým "
    "účinkem unijního práva (eurokonformní/směrnicekonformní výklad). "
    "Odpověz česky přesně ve dvou částech, bez úvodních frází a bez "
    "dalšího textu:\n"
    "HESLO: výstižné právní téma o 1–3 slovech.\n"
    "SHRNUTÍ: nejvýše tři věty – vysvětli, PROČ soud sáhl k eurokonformnímu "
    "(směrnicekonformnímu) výkladu (k jaké směrnici či unijní úpravě měl "
    "vykládané české právo přiblížit a z jakého důvodu) a JAK ho aplikoval "
    "(které ustanovení českého práva takto vyložil a s jakým konkrétním "
    "závěrem). Drž se stručnosti a nic si nevymýšlej."
)

# Prompt pro judikaturu Soudního dvora EU (CJEU) v oblasti IP/IT – pokrývá
# rozsudky/stanoviska i žádosti o rozhodnutí o předběžné otázce (referrals).
CJEU_PROMPT = (
    "Toto je dokument k řízení před Soudním dvorem EU (rozsudek, stanovisko "
    "nebo žádost o rozhodnutí o předběžné otázce) v oblasti práva duševního "
    "vlastnictví nebo IT. Odpověz česky přesně ve dvou částech, bez úvodních "
    "frází a bez dalšího textu:\n"
    "HESLO: výstižné právní téma o 1–3 slovech (např. Ochranná známka, "
    "GDPR, Autorské právo, Doménová jména).\n"
    "SHRNUTÍ: nejvýše tři věty. Jde-li o rozsudek či stanovisko, uveď, jakou "
    "právní otázku Soudní dvůr řešil a jak ji zodpověděl (konkrétní závěr). "
    "Jde-li o žádost o předběžnou otázku (referral), shrň, na co se "
    "předkládající soud Soudního dvora ptá. Drž se stručnosti a nic si "
    "nevymýšlej."
)

# Prompt pro odborný právní článek (z názvu a anotace).
JOURNAL_ARTICLE_PROMPT = (
    "Toto je odborný právní článek (název a anotace). Odpověz česky přesně "
    "ve dvou částech, bez úvodních frází a bez dalšího textu:\n"
    "HESLO: výstižné téma článku o 1–3 slovech.\n"
    "SHRNUTÍ: nejvýše tři věty – o čem článek je a k jakým hlavním závěrům "
    "nebo zjištěním dochází. Drž se stručnosti a nic si nevymýšlej."
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
    "týdny: rozhodnutí Nejvyššího soudu ČR, rozhodnutí a předběžné otázky "
    "Soudního dvora EU a články z právních časopisů.\n\n"
    "Napiš česky přehled toho, co se za ty dva týdny stalo. Vybírej: "
    "především věci relevantní pro praxi v duševním vlastnictví a IT "
    "(autorské právo, ochranné známky, patenty a užitné vzory, průmyslové "
    "vzory, nekalá soutěž, know-how a obchodní tajemství, licence, doménová "
    "jména, ochrana dat, umělá inteligence, platformy), a k tomu pár dalších "
    "skutečně zajímavých věcí, i když do IP nespadají. Většinu položek "
    "vynecháš – vynech vše, co je jen obecné, pro praxi nepodstatné nebo se "
    "týká právních řádů mimo ČR a EU (ledaže jde o věc, která je zajímavá i "
    "odsud). Nic si nevymýšlej, drž se toho, co je ve shrnutích; u čeho si "
    "nejsi jistý, raději vynech.\n\n"
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


def parse_ai_response(raw):
    """Rozparsuje odpověď Gemmy ve tvaru 'HESLO: ...' + 'SHRNUTÍ: ...'.

    Vrací (shrnutí, heslo). Když značky chybí, bere celý text jako shrnutí.
    """
    def clean(s):
        return re.sub(r"\s+", " ", s).strip()

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


def _gemini_generate(parts, max_tokens=4096):
    """Pošle `parts` (text/inline_data) Gemmě a vrátí surový text odpovědi.

    Hlídá rozestup mezi voláními a opakuje při 429/5xx s exponenciálním
    backoffem. Vrací '' při neúspěchu, useknuté odpovědi nebo vypnutém AI.
    """
    global _gemini_last_call
    if not gemini_enabled():
        return ""
    payload = {
        "contents": [{"parts": parts}],
        # Gemma je „thinking" model – necháme vyšší strop, ať se přemýšlení
        # i odpověď vejdou (jinak finishReason MAX_TOKENS).
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_tokens},
    }
    for attempt in range(GEMINI_MAX_RETRIES):
        wait = GEMINI_MIN_INTERVAL - (time.monotonic() - _gemini_last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            r = requests.post(
                GEMINI_URL,
                headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
                json=payload,
                timeout=60,
            )
            _gemini_last_call = time.monotonic()
            if r.status_code in (429, 500, 502, 503):
                backoff = GEMINI_MIN_INTERVAL * (2 ** attempt)
                print(f"    AI {r.status_code}, čekám {backoff:.0f}s (pokus {attempt+1}/{GEMINI_MAX_RETRIES})")
                time.sleep(backoff)
                continue
            r.raise_for_status()
            data = r.json()
            cand = data["candidates"][0]
            parts_out = cand.get("content", {}).get("parts", [])
            # Gemma vrací „thought" části (přemýšlení) i odpověď – bereme jen odpověď.
            raw = "".join(p.get("text", "") for p in parts_out if not p.get("thought")).strip()
            if cand.get("finishReason") == "MAX_TOKENS":
                print(f"    AI: useknuto (MAX_TOKENS), necachuji – '{raw[:40]}…'")
                return ""
            return raw
        except Exception as e:
            _gemini_last_call = time.monotonic()
            backoff = GEMINI_MIN_INTERVAL * (2 ** attempt)
            print(f"    CHYBA AI: {e} – čekám {backoff:.0f}s (pokus {attempt+1}/{GEMINI_MAX_RETRIES})")
            time.sleep(backoff)
            continue
    print("    AI: vyčerpány pokusy, zkusím příště")
    return ""


def gemini_summarize_pdf(pdf_bytes, prompt):
    """Pošle PDF + prompt Gemmě, vrátí (shrnutí, heslo). ('', '') při neúspěchu."""
    if not pdf_bytes:
        return "", ""
    parts = [
        {"inline_data": {
            "mime_type": "application/pdf",
            "data": base64.b64encode(pdf_bytes).decode("ascii"),
        }},
        {"text": prompt},
    ]
    return parse_ai_response(_gemini_generate(parts))


def gemini_summarize_text(text, prompt):
    """Pošle text + prompt Gemmě, vrátí (shrnutí, heslo). ('', '') při neúspěchu."""
    text = (text or "").strip()
    if not text:
        return "", ""
    # Příliš dlouhý text ořízneme – pro shrnutí stačí začátek/podstata.
    if len(text) > 20000:
        text = text[:20000]
    parts = [{"text": prompt + "\n\n--- TEXT ---\n" + text}]
    return parse_ai_response(_gemini_generate(parts))


def gemini_generate_raw(prompt, text, max_tokens=8192):
    """Pošle prompt + text Gemmě a vrátí surovou odpověď bez parsování.

    Pro delší výstupy, které nemají tvar HESLO/SHRNUTÍ (dvoutýdenní přehled).
    Vrací '' při neúspěchu nebo vypnutém AI.
    """
    text = (text or "").strip()
    if not text:
        return ""
    parts = [{"text": prompt + "\n\n--- POLOŽKY ---\n" + text}]
    return _gemini_generate(parts, max_tokens=max_tokens).strip()
