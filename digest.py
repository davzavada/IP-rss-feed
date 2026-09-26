#!/usr/bin/env python3
"""Dvoutýdenní přehled duševního vlastnictví a IT – AI shrnutí shrnutí.

Čte hotová okna z docs/data/ (judikatura, časopisy; běží tedy až po nich),
pošle AI číslovaný seznam položek za poslední dva týdny a nechá si napsat
krátký přehled po tématech. Přehled je jen o IP a IT: z judikatury jdou
rozhodnutí všech soudů zařazená do oblastí duševního vlastnictví a IT
(výchozí oblasti v docs/data/oblasti.json), z časopisů všechno a článek
mimo IP a IT model vynechá stejně jako věci obecné nebo mimo ČR a EU (viz
DIGEST_PROMPT). Přehled je jeden pro všechny, na výběru uživatele nezávisí.

„Poslední dva týdny" se počítají podle toho, kdy položka ve feedu přibyla
(stav prvního výskytu *_seen.json), ne podle data vydání: článek může vyjít
se zpožděním a přesto je novinka. Zdroje samy drží položky déle (časopisy
čtyři týdny, SDEU měsíc), takže bez tohohle filtru by přehled nebyl dvoutýdenní.

Výstup je docs/digest.json, který si vykresluje index.html. Čísla položek,
kterými se model odkazuje na zdroje, se překládají zpět na názvy a odkazy.

Aby se AI nevolala zbytečně, ukládá se otisk vstupu (input_hash). Když se
seznam položek ani jejich shrnutí od minule nezměnily, přehled se negeneruje
znovu a zůstane ležet ten předchozí. Pravidelný pondělní běh tuhle zkratku
vypíná přes DIGEST_FORCE=1 – jednou týdně chceme přehled napsat načisto.

DIGEST_AUTO=1 je režim pro denní spouštění: přehled se napíše, jen když
uložený je z minulého týdne (před pondělím 0:00 pražského času), když
minulý pokus selhal (`selhalo`), nebo když v něm chyběla rozhodnutí, která
teprve čekala na AI rozbor (`cekajici`), a vstup se od té doby změnil.
Výpadek AI v pondělí se tak spraví hned další den.

Když AI přehled nenapíše ani napodruhé, skončí digest.py kódem 1 (workflow
pak ohlásí chybu) a do uloženého přehledu si poznamená `selhalo`.
"""

import hashlib
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from feed_common import (
    DIGEST_PROMPT,
    bez_pravni_formy,
    gemini_enabled,
    gemini_generate_raw,
    load_json,
    save_json,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(BASE_DIR, "docs")
OUTPUT = os.path.join(DOCS_DIR, "digest.json")

WEEKS = 2          # okno přehledu (shodné s oknem feedů)
MAX_ITEMS = 120    # pojistka proti přerostlému promptu
MAX_SOURCES = 6    # kolik odkazů maximálně necháme u jednoho tématu
# Když AI nevrátí použitelný přehled, zkusí se to ještě jednou po chvíli
# (přetížení u Googlu bývá na minuty) – další běh je jinak až za týden.
DIGEST_OPAKOVANI_S = 300
PRAHA = ZoneInfo("Europe/Prague")
# Rozhodnutí, které ještě čeká na AI rozbor, nemá oblasti. Do přehledu se
# i tak vezme, když je duševní vlastnictví zjevné z účastníků (řízení proti
# EUIPO / CPVO u Tribunálu). Ostatní se jen spočítají (`cekajici`).
CEKA_NA_AI = ("pripravuje", "ceka_na_text")
ZJEVNE_IP_RE = re.compile(r"\b(EUIPO|OHIM|CPVO)\b")

# Verze tvaru výstupu. Vstupuje do otisku, takže když do přehledu přibude
# další údaj, uložený přehled se tím sám prohlásí za starý a přegeneruje se
# (jinak by v něm nový údaj chyběl, dokud se nezmění skladba položek).
FORMAT_VERSION = "4"

# Časopisy (docs/data/casopisy.json, píše scraper_journals.py). Klíče zdrojů
# jsou shodné s index.html, aby se štítky obarvily stejně jako v seznamech.
CASOPISY_JSON = os.path.join(DOCS_DIR, "data", "casopisy.json")

# Judikatura z oken pro web (docs/data/judikatura/), jen z oblastí IP/IT.
JUDIKATURA = [
    ("nsoud", "NS", "ns"),
    ("nss", "NSS", "nss"),
    ("us", "ÚS", "us"),
    ("sdeu", "SDEU", "sdeu"),
]


def force_regenerate():
    """DIGEST_FORCE=1 -> přegeneruj i beze změny vstupu (pondělní běh)."""
    return os.environ.get("DIGEST_FORCE", "").strip().lower() in ("1", "true", "yes")


def _first_seen(seen, guid):
    """Kdy položka ve feedu přibyla (podle *_seen.json), nebo None."""
    try:
        return datetime.fromisoformat(seen[guid])
    except (KeyError, TypeError, ValueError):
        return None


def oblasti_ip_it():
    """Oblasti duševního vlastnictví a IT – výchozí oblasti ze seznamu,
    ze kterého čte i web."""
    oblasti = load_json(os.path.join(DOCS_DIR, "data", "oblasti.json")).get("oblasti", [])
    return {o["id"] for o in oblasti if o.get("vychozi")}


def collect_judikatura(now, oldest, stats=None):
    """Rozhodnutí z oken judikatury zařazená do oblastí IP/IT. Senát 23 Cdo
    se nebere celý – jeho obchodní věci do přehledu IP a IT nepatří.

    Rozhodnutí, které zatím čeká na AI rozbor, oblasti nemá; vezme se, jen
    když je IP zjevné z názvu (ZJEVNE_IP_RE), jinak se započítá do
    stats["cekajici"] – ať je vidět, že v přehledu chybí."""
    oblasti = oblasti_ip_it()
    items = []
    cekajici = 0
    for key, label, soud in JUDIKATURA:
        data = load_json(os.path.join(DOCS_DIR, "data", "judikatura", f"{soud}.json"))
        found = 0
        for r in data.get("polozky", []):
            vybrano = bool(set(r.get("oblasti") or []) & oblasti)
            since = _first_seen({"x": r.get("first_seen", "").replace("Z", "+00:00")}, "x")
            if since and since < oldest:
                continue
            if not vybrano and not r.get("oblasti") and r.get("stav_shrnuti") in CEKA_NA_AI:
                if ZJEVNE_IP_RE.search(r.get("nazev") or ""):
                    vybrano = True
                else:
                    cekajici += 1
            if not vybrano:
                continue
            pub_dt = None
            if r.get("zverejneno"):
                try:
                    pub_dt = datetime.fromisoformat(r["zverejneno"]).replace(
                        hour=12, tzinfo=timezone.utc)
                except ValueError:
                    pub_dt = None
            items.append({
                "src": key,
                "src_label": label,
                "tag": "",
                # U ÚS i populární název – modelu napoví, o čem věc je.
                "title": " – ".join(x for x in (r.get("spz"), r.get("nazev")) if x),
                "link": r.get("url", ""),
                "guid": r.get("id", ""),
                "heslo": r.get("heslo", ""),
                "summary": r.get("shrnuti", ""),
                "pub_dt": pub_dt or since,
            })
            found += 1
        print(f"  {label}: {found} rozhodnutí z oblastí IP/IT")
    if cekajici:
        print(f"  Judikatura: {cekajici} rozhodnutí bez oblastí čeká na AI rozbor "
              f"– v přehledu chybí")
    if stats is not None:
        stats["cekajici"] = cekajici
    return items


def collect_casopisy(now, oldest):
    """Články a čísla časopisů z docs/data/casopisy.json (všechny časopisy –
    výchozí výběr je nevynechává)."""
    data = load_json(CASOPISY_JSON)
    zkratky = {c.get("id"): c.get("zkratka", "") for c in data.get("casopisy", [])}
    items, skipped = [], 0
    for r in data.get("polozky", []):
        if not r.get("nazev"):
            continue
        since = _first_seen({"x": (r.get("first_seen") or "").replace("Z", "+00:00")}, "x")
        pub_dt = None
        if r.get("datum"):
            try:
                pub_dt = datetime.fromisoformat(r["datum"]).replace(hour=12, tzinfo=timezone.utc)
            except ValueError:
                pub_dt = None
        # Do okna se položka počítá podle toho, kdy přibyla; když o tom nic
        # nevíme, podle data vydání. Výstřelky do budoucna pryč.
        since = since or pub_dt
        if (since and since < oldest) or (pub_dt and pub_dt > now + timedelta(days=1)):
            skipped += 1
            continue
        # Bez shrnutí aspoň popis (anotace nebo údaje o čísle).
        summary = r.get("shrnuti") or re.sub(r"\s+", " ", r.get("popis", ""))[:400]
        items.append({
            "src": "journals",
            "src_label": "Časopis",
            "tag": zkratky.get(r.get("casopis"), ""),
            "title": r["nazev"],
            "link": r.get("url", ""),
            "guid": r.get("id", r["nazev"]),
            "heslo": r.get("heslo", ""),
            "summary": summary,
            "pub_dt": pub_dt or since,
        })
    print(f"  Časopisy: {len(items)} položek"
          + (f" ({skipped} mimo okno {WEEKS} týdnů)" if skipped else ""))
    return items


def collect_items(stats=None):
    """Načte položky ze všech feedů za okno WEEKS, seřazené od nejnovější.

    Vrací seznam dictů se zdrojem, názvem, odkazem, datem, heslem a shrnutím.
    Položky bez shrnutí bere taky – model má aspoň název a popis.
    stats – volitelný dict, do kterého se zapíše počet čekajících rozhodnutí.
    """
    now = datetime.now(timezone.utc)
    oldest = now - timedelta(weeks=WEEKS)
    items = collect_judikatura(now, oldest, stats)

    items += collect_casopisy(now, oldest)

    # Položky bez data (neměly by být) řadíme na konec.
    items.sort(key=lambda i: i["pub_dt"] or oldest, reverse=True)
    return items[:MAX_ITEMS]


def build_prompt_input(items):
    """Očísluje položky do textového seznamu pro model (čísla = 1..N)."""
    lines = []
    for n, it in enumerate(items, start=1):
        head = f"{n}. [{it['src_label']}"
        if it["tag"] and it["tag"] != it["src_label"]:
            head += f" / {it['tag']}"
        if it["pub_dt"]:
            head += f", {it['pub_dt'].strftime('%d.%m.%Y')}"
        head += f"] {it['title']}"
        if it["heslo"]:
            head += f" — heslo: {it['heslo']}"
        lines.append(head)
        if it["summary"]:
            lines.append(f"   {it['summary']}")
    return "\n".join(lines)


def input_hash(items):
    """Otisk vstupu – když se nezmění, není co přegenerovávat."""
    h = hashlib.sha256()
    h.update(f"v{FORMAT_VERSION}\n".encode("utf-8"))
    for it in items:
        h.update(f"{it['guid']}|{it['summary']}\n".encode("utf-8"))
    return h.hexdigest()


def _strip_markdown(text):
    """Odstraní markdown ozdoby, které modely občas přidají (**TÉMA:**, odrážky).

    Dělá se to před parsováním, jinak by se značky TÉMA/TEXT/ZDROJE nenašly.
    """
    text = re.sub(r"\*\*|__|`", "", text)
    return re.sub(r"^[ \t]*[-*#>]+[ \t]*", "", text, flags=re.MULTILINE)


def _clean(text):
    """Sjednotí bílé znaky do jednoho odstavce a odstraní právní formy
    (s.r.o., a. s.…), které přehled psát nemá."""
    return bez_pravni_formy(re.sub(r"\s+", " ", text).strip())


def parse_digest(raw, items):
    """Rozparsuje odpověď (PŘEHLED / TÉMA / TEXT / ZDROJE) na intro a bloky.

    Čísla ve ZDROJE překládá zpět na položky; čísla mimo rozsah ignoruje.
    """
    if not raw:
        return "", []

    chunks = re.split(r"\n\s*T[ÉE]MA\s*:\s*", "\n" + _strip_markdown(raw))

    intro = ""
    m = re.search(r"P[ŘR]EHLED\s*:\s*(.*)", chunks[0], re.DOTALL | re.IGNORECASE)
    if m:
        intro = _clean(m.group(1))
    elif chunks[0].strip():
        # Model vynechal značku – ber úvodní text tak, jak je.
        intro = _clean(chunks[0])

    blocks = []
    for chunk in chunks[1:]:
        title = _clean(chunk.split("\n", 1)[0])
        rest = chunk.split("\n", 1)[1] if "\n" in chunk else ""

        mt = re.search(r"TEXT\s*:\s*(.*?)(?=\n\s*ZDROJE\s*:|$)", rest,
                       re.DOTALL | re.IGNORECASE)
        text = _clean(mt.group(1)) if mt else _clean(rest)
        if not title or not text:
            continue

        sources = []
        ms = re.search(r"ZDROJE\s*:\s*(.*)", rest, re.IGNORECASE)
        if ms:
            used = []
            for num in re.findall(r"\d+", ms.group(1)):
                idx = int(num)
                if 1 <= idx <= len(items) and idx not in used:
                    used.append(idx)
            for idx in used[:MAX_SOURCES]:
                it = items[idx - 1]
                sources.append({
                    "src": it["src"],
                    "label": it["src_label"],
                    # Zkratka časopisu / typu řízení ([JIPLP], [Ruling], …) –
                    # u článků je hlavní informace, ze kterého časopisu jsou.
                    "tag": it["tag"],
                    "title": it["title"],
                    "link": it["link"],
                })

        blocks.append({"title": title, "text": text, "sources": sources})

    return intro, blocks


def zacatek_tydne(now):
    """Pondělí 0:00 pražského času v týdnu, do kterého patří `now` (UTC)."""
    mistni = now.astimezone(PRAHA)
    pondeli = (mistni - timedelta(days=mistni.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return pondeli.astimezone(timezone.utc)


def proc_generovat(previous, ihash, now):
    """Režim DIGEST_AUTO: důvod, proč přehled napsat znovu, nebo None."""
    try:
        generated = datetime.fromisoformat(previous["generated"])
    except (KeyError, TypeError, ValueError):
        return "přehled zatím není"
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    if not previous.get("blocks"):
        return "uložený přehled je prázdný"
    if previous.get("selhalo"):
        return "minulý pokus selhal"
    if generated < zacatek_tydne(now):
        return "přehled je z minulého týdne"
    if previous.get("cekajici") and previous.get("input_hash") != ihash:
        return "rozhodnutí, která v přehledu chyběla, mezitím mohla projít AI"
    return None


def main():
    print("Sestavuji dvoutýdenní přehled...")
    stats = {}
    items = collect_items(stats)
    cekajici = stats.get("cekajici", 0)
    print(f"Celkem {len(items)} položek za poslední {WEEKS} týdny")

    now = datetime.now(timezone.utc)
    previous = load_json(OUTPUT, default={})
    digest_input = build_prompt_input(items)
    ihash = input_hash(items)

    if not items:
        save_json(OUTPUT, {
            "generated": now.isoformat(),
            "from": (now - timedelta(weeks=WEEKS)).date().isoformat(),
            "to": now.date().isoformat(),
            "total": 0, "covered": 0, "cekajici": cekajici,
            "intro": "", "blocks": [], "input_hash": ihash,
        })
        print("Žádné položky – přehled je prázdný")
        return

    auto = os.environ.get("DIGEST_AUTO", "").strip().lower() in ("1", "true", "yes")
    if auto and not force_regenerate():
        duvod = proc_generovat(previous, ihash, now)
        if not duvod:
            print("Přehled je aktuální – ponechávám ho")
            return
        print(f"Generuji přehled: {duvod}")
    elif previous.get("input_hash") == ihash and previous.get("blocks"):
        if not force_regenerate():
            print("Vstup se nezměnil – přehled ponechávám beze změny")
            return
        print("Vstup se nezměnil, ale DIGEST_FORCE=1 – generuji znovu")

    if not gemini_enabled():
        print("AI je vypnutá – přehled ponechávám beze změny")
        return

    blocks = []
    for pokus in range(2):
        if pokus:
            print(f"AI nevrátila použitelný přehled – zkusím to znovu za "
                  f"{DIGEST_OPAKOVANI_S // 60} min")
            time.sleep(DIGEST_OPAKOVANI_S)
        raw = gemini_generate_raw(DIGEST_PROMPT, digest_input)
        intro, blocks = parse_digest(raw, items)
        if blocks:
            break
    if not blocks:
        # Prázdný výstup nemá cenu ukládat – starý přehled je pořád lepší
        # než nic. Selhání se ale poznamená (DIGEST_AUTO to příště zkusí
        # znovu) a běh skončí chybou, ať o tom přijde upozornění.
        if previous:
            save_json(OUTPUT, dict(previous, selhalo=now.isoformat()))
        print("::error::AI nevrátila použitelný přehled – ponechávám ten předchozí")
        sys.exit(1)

    covered = len({s["title"] for b in blocks for s in b["sources"]})
    save_json(OUTPUT, {
        "generated": now.isoformat(),
        "from": (now - timedelta(weeks=WEEKS)).date().isoformat(),
        "to": now.date().isoformat(),
        "total": len(items),
        "covered": covered,
        "cekajici": cekajici,
        "intro": intro,
        "blocks": blocks,
        "input_hash": ihash,
    })

    print(f"Přehled zapsán do {OUTPUT} – témat: {len(blocks)}, "
          f"položek v přehledu: {covered} z {len(items)}")
    for b in blocks:
        print(f"  - {b['title']} (zdrojů: {len(b['sources'])})")


if __name__ == "__main__":
    main()
