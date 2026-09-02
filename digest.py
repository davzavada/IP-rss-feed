#!/usr/bin/env python3
"""Dvoutýdenní přehled – AI shrnutí shrnutí ze všech feedů dohromady.

Čte hotové feedy z docs/ (běží tedy až po scraperech), pošle Gemmě číslovaný
seznam položek za poslední dva týdny a nechá si napsat krátký přehled po
tématech: hlavně to, co je relevantní pro praxi v IP/IT, plus pár dalších
zajímavostí. Velká část položek se do přehledu nedostane – obecné věci, věci
mimo praxi nebo mimo ČR a EU model vynechává (viz DIGEST_PROMPT).

„Poslední dva týdny" se počítají podle toho, kdy položka ve feedu přibyla
(stav prvního výskytu *_seen.json), ne podle data vydání: článek může vyjít
se zpožděním a přesto je novinka. Feedy samy drží položky déle (časopisy
čtyři týdny, CJEU osm), takže bez tohohle filtru by přehled nebyl dvoutýdenní.

Výstup je docs/digest.json, který si vykresluje index.html. Čísla položek,
kterými se model odkazuje na zdroje, se překládají zpět na názvy a odkazy.

Aby se AI nevolala zbytečně, ukládá se otisk vstupu (input_hash). Když se
seznam položek ani jejich shrnutí od minule nezměnily, přehled se negeneruje
znovu a zůstane ležet ten předchozí. Pravidelný pondělní běh tuhle zkratku
vypíná přes DIGEST_FORCE=1 – jednou týdně chceme přehled napsat načisto.
"""

import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from xml.etree.ElementTree import parse as parse_xml

from feed_common import (
    DIGEST_PROMPT,
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

# Verze tvaru výstupu. Vstupuje do otisku, takže když do přehledu přibude
# další údaj, uložený přehled se tím sám prohlásí za starý a přegeneruje se
# (jinak by v něm nový údaj chyběl, dokud se nezmění skladba položek).
FORMAT_VERSION = "2"

# (klíč zdroje, štítek, soubor feedu, stav prvního výskytu) – klíče jsou
# shodné s index.html, aby se štítky obarvily stejně jako v seznamech.
SOURCES = [
    ("nsoud", "NS 23 Cdo", "feed.xml", "feed_seen.json"),
    ("cjeu", "CJEU", "ipcuria_feed.xml", "ipcuria_seen.json"),
    ("journals", "Časopis", "journals_feed.xml", "journals_seen.json"),
]


def force_regenerate():
    """DIGEST_FORCE=1 -> přegeneruj i beze změny vstupu (pondělní běh)."""
    return os.environ.get("DIGEST_FORCE", "").strip().lower() in ("1", "true", "yes")


def _text(el, tag):
    """Text podelementu, nebo '' když chybí."""
    child = el.find(tag)
    return (child.text or "").strip() if child is not None and child.text else ""


def _parse_pub_date(value):
    """RFC 822 pubDate -> datetime. Vrací None, když se nepodaří."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%a, %d %b %Y %H:%M:%S %z")
    except ValueError:
        return None


def _first_seen(seen, guid):
    """Kdy položka ve feedu přibyla (podle *_seen.json), nebo None."""
    try:
        return datetime.fromisoformat(seen[guid])
    except (KeyError, TypeError, ValueError):
        return None


def collect_items():
    """Načte položky ze všech feedů za okno WEEKS, seřazené od nejnovější.

    Vrací seznam dictů se zdrojem, názvem, odkazem, datem, heslem a shrnutím.
    Položky bez shrnutí bere taky – model má aspoň název a popis.
    """
    now = datetime.now(timezone.utc)
    oldest = now - timedelta(weeks=WEEKS)
    items = []

    for key, label, filename, seen_file in SOURCES:
        path = os.path.join(DOCS_DIR, filename)
        if not os.path.exists(path):
            print(f"  Feed {filename} chybí, přeskakuji")
            continue
        try:
            root = parse_xml(path).getroot()
        except Exception as e:
            print(f"  CHYBA čtení {filename}: {e}")
            continue
        seen = load_json(os.path.join(BASE_DIR, seen_file))

        found = skipped = 0
        for el in root.iter("item"):
            title = _text(el, "title")
            if not title:
                continue
            guid = _text(el, "guid") or title
            pub_dt = _parse_pub_date(_text(el, "pubDate"))
            # Do okna se položka počítá podle toho, kdy přibyla; když o tom
            # stav nic neví, podle data vydání. Výstřelky do budoucna pryč.
            since = _first_seen(seen, guid) or pub_dt
            if (since and since < oldest) or (pub_dt and pub_dt > now + timedelta(days=1)):
                skipped += 1
                continue

            summary = _text(el, "ai-summary")
            if not summary:
                # Fallback: popis z feedu (obsahuje anotaci nebo metadata).
                summary = re.sub(r"\s+", " ", _text(el, "description"))[:400]

            items.append({
                "src": key,
                "src_label": label,
                # Štítek typu/časopisu ([DV], [Ruling], …) držíme zvlášť,
                # ať se název čte stejně jako v tabulkách na stránce.
                "tag": (re.match(r"^\[([^\]]+)\]", title) or [None, ""])[1],
                "title": re.sub(r"^\[[^\]]+\]\s*", "", title),
                "link": _text(el, "link"),
                "guid": guid,
                "heslo": _text(el, "ai-tag"),
                "summary": summary,
                "pub_dt": pub_dt,
            })
            found += 1
        print(f"  {label}: {found} položek"
              + (f" ({skipped} mimo okno {WEEKS} týdnů)" if skipped else ""))

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
    """Odstraní markdown ozdoby, které Gemma občas přidá (**TÉMA:**, odrážky).

    Dělá se to před parsováním, jinak by se značky TÉMA/TEXT/ZDROJE nenašly.
    """
    text = re.sub(r"\*\*|__|`", "", text)
    return re.sub(r"^[ \t]*[-*#>]+[ \t]*", "", text, flags=re.MULTILINE)


def _clean(text):
    """Sjednotí bílé znaky do jednoho odstavce."""
    return re.sub(r"\s+", " ", text).strip()


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


def main():
    print("Sestavuji dvoutýdenní přehled...")
    items = collect_items()
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
            "total": 0, "covered": 0,
            "intro": "", "blocks": [], "input_hash": ihash,
        })
        print("Žádné položky – přehled je prázdný")
        return

    if previous.get("input_hash") == ihash and previous.get("blocks"):
        if not force_regenerate():
            print("Vstup se nezměnil – přehled ponechávám beze změny")
            return
        print("Vstup se nezměnil, ale DIGEST_FORCE=1 – generuji znovu")

    if not gemini_enabled():
        print("AI je vypnutá – přehled ponechávám beze změny")
        return

    raw = gemini_generate_raw(DIGEST_PROMPT, digest_input)
    intro, blocks = parse_digest(raw, items)
    if not blocks:
        # Prázdný výstup nemá cenu ukládat – starý přehled je pořád lepší
        # než nic a příští běh to zkusí znovu.
        print("AI nevrátila použitelný přehled – ponechávám ten předchozí")
        return

    covered = len({s["title"] for b in blocks for s in b["sources"]})
    save_json(OUTPUT, {
        "generated": now.isoformat(),
        "from": (now - timedelta(weeks=WEEKS)).date().isoformat(),
        "to": now.date().isoformat(),
        "total": len(items),
        "covered": covered,
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
