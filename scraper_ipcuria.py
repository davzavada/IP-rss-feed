#!/usr/bin/env python3
"""Scraper IPcuria – generuje RSS feed ze 2 kategorií (rulings, referrals)."""

import os
import re
from datetime import datetime, timezone, timedelta
from xml.etree.ElementTree import Element, SubElement, ElementTree, indent

import requests
from bs4 import BeautifulSoup

from feed_common import (
    CJEU_PROMPT,
    filter_by_first_seen,
    gemini_enabled,
    gemini_summarize_text,
    load_json,
    prune_meta,
    save_json,
)

CURIA_BASE = "https://curia.europa.eu/juris/liste.do?num="
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "ipcuria_feed.xml")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ipcuria_seen.json")
# Cache AI shrnutí podle guid ({guid: {"summary": ..., "tag": ...}}).
META_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ipcuria_meta.json")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

SOURCES = [
    ("https://ipcuria.eu/all_preliminary_rulings.php", "Ruling"),
    ("https://ipcuria.eu/all_referrals.php", "Referral"),
]

# CJEU chodí po kapkách – zvlášť u referralů se stane, že za dva týdny nepřibude
# nic a tabulka zůstane prázdná. Okno je proto delší než u ostatních feedů.
WINDOW_WEEKS = 8    # ~dva měsíce od prvního výskytu položky
# Stahovací okno musí být delší než to zobrazované: feed se pokaždé staví
# znovu ze staženého seznamu, takže co sem nedosáhne, ve feedu není – i když
# to bylo poprvé viděno včas. Pár dnů navíc kryje zpožděné zveřejnění.
FETCH_DAYS = WINDOW_WEEKS * 7 + 14


def _guid(d):
    """Stabilní identifikátor položky (shodný s oknem prvního výskytu i RSS guid)."""
    return f"{d['category']}-{d['case_ref']}"


def fetch_all():
    """Stáhne obě stránky a vrátí rozhodnutí z posledních FETCH_DAYS dnů."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=FETCH_DAYS)
    decisions = []

    for url, category in SOURCES:
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"  CHYBA při stahování {category} ({url}): {e}")
            continue
        body_html = resp.text

        # Stránky mají bloky oddělené <hr>
        blocks = re.split(r"<hr\s*/?>", body_html)

        for block in blocks:
            block_soup = BeautifulSoup(block, "html.parser")
            text = block_soup.get_text()

            # Číslo případu z odkazu
            link_tag = block_soup.find("a", href=re.compile(r"case\?reference="))
            if not link_tag:
                continue
            case_ref = link_tag.get_text(strip=True)

            # Název případu z <i>
            name_tag = block_soup.find("i")
            case_name = name_tag.get_text(strip=True) if name_tag else ""

            # Datum – různé formáty:
            # "Judgement of 19 Mar 2026" / "Order of ..." / "lodged on 3 Feb 2026"
            date_match = re.search(r"(\d{1,2}\s+\w{3}\s+\d{4})", text)
            if not date_match:
                continue

            date_str = date_match.group(1)
            try:
                dt = datetime.strptime(date_str, "%d %b %Y").replace(tzinfo=timezone.utc)
            except ValueError:
                continue

            if dt < cutoff:
                continue

            # Typ rozhodnutí (pro Ruling/Appeal stránky)
            type_match = re.search(r"(Judgement|Judgment|Order)", text)
            detail_type = ""
            if type_match:
                detail_type = type_match.group(1)
                if detail_type == "Judgement":
                    detail_type = "Judgment"

            # Kategorie z breadcrumbs (pokud existují)
            categories = []
            for span in block_soup.select("span.breadcrumbs"):
                cats = [a.get_text(strip=True) for a in span.find_all("a")]
                if cats:
                    categories.append(" > ".join(cats))

            decisions.append({
                "case_ref": case_ref,
                "case_name": case_name,
                "date": dt,
                "date_str": date_str,
                "category": category,
                "detail_type": detail_type,
                "categories": categories,
                "ipcuria_url": f"https://ipcuria.eu/case?reference={case_ref}",
                "curia_url": f"{CURIA_BASE}{case_ref}",
            })

    decisions.sort(key=lambda d: d["date"], reverse=True)
    return decisions


# Hláška ipcuria u referralů, jejichž otázky ještě nejsou zveřejněné –
# takovou stránku nemá smysl shrnovat (žádný obsah k popsání).
NO_QUESTIONS_MARKER = "questions are not yet available"

# Když ipcuria otázky nemá, zkusíme oznámení o žádosti v Úředním věstníku.
# Web CURIA i EUR-Lexu je pro scraper zavřený (curia.europa.eu vrací z runneru
# jen prázdný Angular shell a hlášku „site is temporarily unavailable“,
# EUR-Lex robot-check), ale Cellar – datové API Úřadu pro publikace – text
# oznámení vydá. Adresuje se přes CELEX: 6<rok>CN<číslo> je oznámení o věci
# zapsané u Soudního dvora, a jeho text obsahuje i položené otázky.
# Dokud oznámení nevyjde, Cellar vrací 404 a ve feedu zůstane poznámka.
CELEX_URL = "http://publications.europa.eu/resource/celex/{celex}"
CELEX_HEADERS = {
    "User-Agent": "pravni-rss-feed/1.0 (+https://rss.davidzavada.cz)",
    "Accept": "application/xhtml+xml",
    "Accept-Language": "eng",
}
CASE_REF_RE = re.compile(r"^C-(\d+)/(\d{2})$")
QUESTIONS_PENDING_NOTE = "Otázky zatím nezveřejněny."


def _celex(case_ref):
    """CELEX oznámení k případu: 'C-691/26' -> '62026CN0691'. Jinak ''."""
    match = CASE_REF_RE.match(case_ref.strip())
    if not match:
        return ""
    num, year = match.groups()
    return f"620{year}CN{int(num):04d}"


def fetch_eurlex_notice(case_ref):
    """Text oznámení o žádosti z Úředního věstníku (přes Cellar), nebo ''."""
    celex = _celex(case_ref)
    if not celex:
        return ""
    try:
        r = requests.get(CELEX_URL.format(celex=celex), headers=CELEX_HEADERS, timeout=45)
    except Exception as e:
        print(f"    [diag] {case_ref}: CHYBA stahování EUR-Lex: {e}")
        return ""
    if r.status_code == 404:  # oznámení ještě nevyšlo
        return ""
    if not r.ok:
        print(f"    [diag] {case_ref}: EUR-Lex {celex} vrátil {r.status_code}")
        return ""

    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def fetch_case_text(ipcuria_url):
    """Stáhne stránku případu na ipcuria.eu a vrátí její text.

    ipcuria.eu u rozsudků zveřejňuje plný text rozhodnutí (desítky tisíc
    znaků) i s tématy/hesly v záhlaví; u žádostí o předběžnou otázku buď
    položené otázky, nebo upozornění, že otázky zatím nejsou k dispozici.
    CURIA (liste.do) je proti tomu vykreslovaná JavaScriptem a prostý
    request z ní žádný text rozhodnutí nedostane. Vrací '' při neúspěchu.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
    }
    try:
        r = requests.get(ipcuria_url, headers=headers, timeout=60)
        r.raise_for_status()
    except Exception as e:
        print(f"    CHYBA stahování ipcuria {ipcuria_url}: {e}")
        return ""

    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def _trim_for_summary(text, head=12000, tail=6000):
    """Dlouhé rozsudky ořízneme na začátek (předmět sporu, právní otázka) a
    konec (výrok soudu), aby se do shrnutí dostalo obojí, ne jen úvod."""
    text = text.strip()
    if len(text) <= head + tail:
        return text
    return text[:head] + "\n…\n" + text[-tail:]


MIN_TEXT_FOR_SUMMARY = 500  # pod to už není co shrnovat (ať si Gemma nevymýšlí)


def case_text_for_summary(d):
    """Text k shrnutí a jeho zdroj – ipcuria, jinak oznámení v ÚV.

    U rozsudků má ipcuria plný text rozhodnutí. U žádostí o předběžnou otázku
    tam ale často stojí jen „questions are not yet available“; tehdy zkusíme
    oznámení o věci v Úředním věstníku, které otázky obsahuje. Než oznámení
    vyjde, není k dispozici ani jedno – vrací se ('', '').
    """
    text = fetch_case_text(d["ipcuria_url"])
    pending = NO_QUESTIONS_MARKER in text.lower()
    print(f"    [diag] {_guid(d)}: {len(text.strip())} znaků textu z ipcuria"
          + (" (otázky nezveřejněny)" if pending else ""))
    if not pending and len(text.strip()) >= MIN_TEXT_FOR_SUMMARY:
        return text, "ipcuria"

    notice = fetch_eurlex_notice(d["case_ref"])
    if len(notice) >= MIN_TEXT_FOR_SUMMARY:
        print(f"    [diag] {_guid(d)}: {len(notice)} znaků z oznámení v ÚV (EUR-Lex)")
        return notice, "eur-lex"
    if pending:
        print(f"    [diag] {_guid(d)}: oznámení v ÚV zatím nevyšlo")
    return "", ""


def enrich_summaries(decisions):
    """Doplní AI shrnutí (HESLO + SHRNUTÍ) přes Gemma; cache podle guid.

    Volá se až na ponechané položky (po okně), aby se neshrnovalo zbytečně.
    Když není z čeho shrnovat, protože žádost o předběžnou otázku ještě nemá
    zveřejněné otázky, uloží se místo shrnutí poznámka – ať je ve feedu vidět,
    že tam shrnutí nechybí omylem. Poznámka není konečná: dokud položka
    zůstane v okně, zkouší se to při každém běhu znovu.
    """
    meta = load_json(META_FILE)

    def apply(d, m):
        d["summary"] = m.get("summary", "")
        d["tag"] = m.get("tag", "")
        d["note"] = "" if m.get("summary") else m.get("note", "")

    if not gemini_enabled():
        for d in decisions:
            apply(d, meta.get(_guid(d), {}))
        return decisions

    summarized = 0
    calls = 0
    for d in decisions:
        g = _guid(d)
        m = meta.get(g, {})
        if not m.get("summary"):
            text, source = case_text_for_summary(d)
            if text:
                calls += 1
                summary, tag = gemini_summarize_text(_trim_for_summary(text), CJEU_PROMPT)
                if summary:
                    m = {"summary": summary, "tag": tag, "source": source}
                    meta[g] = m
                    summarized += 1
                else:
                    print(f"    [diag] {g}: Gemma nevrátila shrnutí")
            elif d["category"] == "Referral":
                m = {"note": QUESTIONS_PENDING_NOTE}
                meta[g] = m
        apply(d, m)

    save_json(META_FILE, meta)
    if calls:
        print(f"  AI: {summarized}/{calls} shrnutí vygenerováno")
    return decisions


def build_rss(decisions):
    """Vytvoří RSS 2.0 XML z rozhodnutí."""
    rss = Element("rss", version="2.0", attrib={
        "xmlns:dc": "http://purl.org/dc/elements/1.1/",
        "xmlns:atom": "http://www.w3.org/2005/Atom",
    })
    channel = SubElement(rss, "channel")

    SubElement(channel, "title").text = "CJEU IP case law"
    SubElement(channel, "link").text = "https://curia.europa.eu/"
    # atom:link rel="self" – adresa feedu samotného (vyžaduje RSS best practice)
    SubElement(channel, "atom:link", attrib={
        "href": "https://rss.davidzavada.cz/ipcuria_feed.xml",
        "rel": "self", "type": "application/rss+xml",
    })
    SubElement(channel, "description").text = (
        "Latest CJEU IP case law: preliminary rulings, referrals, appeals"
    )
    SubElement(channel, "language").text = "en"
    SubElement(channel, "lastBuildDate").text = datetime.now(timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )

    for d in decisions:
        item = SubElement(channel, "item")

        title = f"[{d['category']}] {d['case_ref']}"
        if d["case_name"]:
            title += f" ({d['case_name']})"
        SubElement(item, "title").text = title

        SubElement(item, "link").text = d["curia_url"]
        SubElement(item, "guid", isPermaLink="false").text = f"{d['category']}-{d['case_ref']}"

        if d.get("is_new"):
            SubElement(item, "is-new").text = "true"

        # AI shrnutí + heslo (čte je index.html do samostatných sloupců)
        if d.get("tag"):
            SubElement(item, "ai-tag").text = d["tag"]
        if d.get("summary"):
            SubElement(item, "ai-summary").text = d["summary"]
        # Místo shrnutí poznámka, proč tam žádné není (čte ji i index.html).
        if d.get("note"):
            SubElement(item, "note").text = d["note"]

        desc_parts = [
            f"[{d['category']}] {d['date_str']}, {d['case_ref']}",
        ]
        if d["case_name"]:
            desc_parts[0] += f" ({d['case_name']})"
        if d.get("summary"):
            desc_parts.append(d["summary"])
        elif d.get("note"):
            desc_parts.append(d["note"])
        if d["detail_type"]:
            desc_parts.append(f"Type: {d['detail_type']}")
        for cat in d["categories"]:
            desc_parts.append(f"- {cat}")
        desc_parts.append(f"CURIA: {d['curia_url']}")

        SubElement(item, "description").text = "\n".join(desc_parts)

        SubElement(item, "pubDate").text = d["date"].strftime(
            "%a, %d %b %Y 12:00:00 +0000"
        )
        SubElement(item, "dc:date").text = d["date"].strftime("%Y-%m-%d")

    return rss


def main():
    print("Stahuji IPcuria – 2 kategorie...")
    decisions = fetch_all()
    print(f"Nalezeno {len(decisions)} položek za posledních {FETCH_DAYS} dnů")

    # Okno od prvního výskytu + příznak „nové dnes" (jednotné se zbytkem)
    decisions = filter_by_first_seen(decisions, _guid, STATE_FILE, weeks=WINDOW_WEEKS)
    decisions.sort(key=lambda d: d["date"], reverse=True)
    print(f"Po okně {WINDOW_WEEKS} týdnů: {len(decisions)} položek")

    decisions = enrich_summaries(decisions)  # AI shrnutí jen na ponechané

    # Cache shrnutí prořízneme podle stavu prvního výskytu, ať neroste donekonečna
    save_json(META_FILE, prune_meta(load_json(META_FILE), STATE_FILE))

    for d in decisions:
        print(f"  [{d['category']}] {d['case_ref']} {d['case_name']} ({d['date_str']})")

    rss = build_rss(decisions)

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    indent(rss, space="  ")
    tree = ElementTree(rss)
    tree.write(OUTPUT, encoding="unicode", xml_declaration=True)
    print(f"RSS feed zapsán do {OUTPUT}")


if __name__ == "__main__":
    main()
