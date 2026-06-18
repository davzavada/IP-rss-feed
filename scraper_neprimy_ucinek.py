#!/usr/bin/env python3
"""Scraper úřední desky NS ČR – hledá rozhodnutí s odkazy na nepřímý účinek unijního práva."""

import io
import os
import re
from datetime import datetime, timezone
from xml.etree.ElementTree import Element, SubElement, ElementTree, indent

import requests
from bs4 import BeautifulSoup
import pdfplumber

from feed_common import (
    JUDIKATURA_PROMPT,
    filter_by_first_seen,
    gemini_enabled,
    gemini_summarize_text,
    load_json,
    save_json,
)

URL = "https://www.nsoud.cz/uredni-deska/obcanskopravni-a-obchodni-kolegium/vyhlasovana-rozhodnuti"
BASE_URL = "https://www.nsoud.cz"
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "neprimy_ucinek_feed.xml")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "neprimy_seen.json")
META_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "neprimy_meta.json")


def _guid(d):
    """Stabilní identifikátor rozhodnutí (shodný s oknem prvního výskytu)."""
    return d.get("pdf_url") or d["case_number"]


def enrich_summaries(decisions):
    """Doplní AI shrnutí (právní otázka a její řešení) přes Gemma; cache podle guid.

    Text rozhodnutí už máme z fáze hledání klíčových slov (d["text"]),
    takže PDF znovu nestahujeme. Volá se až na ponechané položky.
    """
    meta = load_json(META_FILE)
    if not gemini_enabled():
        for d in decisions:
            m = meta.get(_guid(d), {})
            d["summary"] = m.get("summary", "")
            d["tag"] = m.get("tag", "")
        return decisions

    summarized = 0
    calls = 0
    for d in decisions:
        g = _guid(d)
        m = meta.get(g, {})
        if not m.get("summary") and d.get("text"):
            calls += 1
            summary, tag = gemini_summarize_text(d["text"], JUDIKATURA_PROMPT)
            if summary:
                m = {"summary": summary, "tag": tag}
                meta[g] = m
                summarized += 1
        d["summary"] = m.get("summary", "")
        d["tag"] = m.get("tag", "")

    save_json(META_FILE, meta)
    if calls:
        print(f"  AI: {summarized}/{calls} shrnutí vygenerováno")
    return decisions

# Regex vzory pro nepřímý účinek – pokrývají skloňování
KEYWORD_PATTERNS = [
    # nepřímý účinek (všechny pády)
    (r"nepřím\w*\s+účin\w*", "nepřímý účinek"),
    # eurokonformní výklad/interpretace
    (r"eurokonformní\w*\s+(?:výklad\w*|interpretac\w*)", "eurokonformní výklad"),
    # konformní výklad/interpretace (bez "euro" prefixu)
    (r"konformní\w*\s+(?:výklad\w*|interpretac\w*)", "konformní výklad"),
    # směrnicekonformní
    (r"směrnicekonformní\w*", "směrnicekonformní"),
    # unijně konformní
    (r"unijně\s+konformní\w*", "unijně konformní"),
    # výklad v souladu se směrnicí / s unijním právem / s právem EU
    (r"výklad\w*\s+v\s+souladu\s+(?:se\s+směrnicí|s\s+unijní\w*\s+práv\w*|s\s+práv\w*\s+(?:EU|unie))", "výklad v souladu"),
    # nepřímá aplikace
    (r"nepřím\w*\s+aplikac\w*", "nepřímá aplikace"),
    # interpretační povinnost
    (r"interpretační\w*\s+povinnost\w*", "interpretační povinnost"),
    # povinnost konformního výkladu
    (r"povinnost\w*\s+konformní\w*\s+výklad\w*", "povinnost konformního výkladu"),
    # Klíčové judikáty SDEU
    (r"marleasing", "Marleasing"),
    (r"von\s+colson", "Von Colson"),
    (r"pfeiffer", "Pfeiffer"),
    (r"kolpinghuis", "Kolpinghuis"),
    (r"adeneler", "Adeneler"),
]


def fetch_all_decisions():
    """Stáhne stránku a vrátí všechna rozhodnutí civilního kolegia."""
    resp = requests.get(URL, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    decisions = []
    for row in soup.select("table tr"):
        cells = row.find_all("td")
        if len(cells) < 3:
            continue

        case_number = cells[0].get_text(strip=True)
        case_normalized = re.sub(r"\s+", " ", case_number)
        date_text = cells[1].get_text(strip=True)

        pdf_link = ""
        link_tag = row.find("a", href=True)
        if link_tag:
            href = link_tag["href"]
            pdf_link = href if href.startswith("http") else BASE_URL + href

        decisions.append({
            "case_number": case_normalized,
            "date": date_text,
            "pdf_url": pdf_link,
        })

    return decisions


def extract_pdf_text(url):
    """Stáhne PDF a extrahuje text."""
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
            text = ""
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
        return text
    except Exception as e:
        print(f"  Chyba při čtení PDF {url}: {e}")
        return ""


def find_keywords(text):
    """Najde klíčová slova v textu pomocí regex, vrátí seznam nalezených."""
    text_lower = text.lower()
    found = []
    for pattern, label in KEYWORD_PATTERNS:
        if re.search(pattern, text_lower):
            found.append(label)
    return found


def build_rss(decisions):
    """Vytvoří RSS 2.0 XML z rozhodnutí."""
    rss = Element("rss", version="2.0", attrib={
        "xmlns:dc": "http://purl.org/dc/elements/1.1/"
    })
    channel = SubElement(rss, "channel")

    SubElement(channel, "title").text = "NS ČR – nepřímý účinek v rozhodnutích"
    SubElement(channel, "link").text = URL
    SubElement(channel, "description").text = (
        "Rozhodnutí civilního kolegia NS ČR obsahující odkazy na nepřímý účinek unijního práva"
    )
    SubElement(channel, "language").text = "cs"
    SubElement(channel, "lastBuildDate").text = datetime.now(timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )

    for d in decisions:
        item = SubElement(channel, "item")
        SubElement(item, "title").text = d["case_number"]

        if d["pdf_url"]:
            SubElement(item, "link").text = d["pdf_url"]
            SubElement(item, "guid", isPermaLink="true").text = d["pdf_url"]
        else:
            SubElement(item, "guid", isPermaLink="false").text = d["case_number"]

        if d.get("is_new"):
            SubElement(item, "is-new").text = "true"

        if d.get("tag"):
            SubElement(item, "ai-tag").text = d["tag"]
        if d.get("summary"):
            SubElement(item, "ai-summary").text = d["summary"]

        kw_str = ", ".join(d["found_keywords"])
        desc = f"Rozhodnutí {d['case_number']} vyhlášeno {d['date']}\n"
        if d.get("summary"):
            desc += f"{d['summary']}\n"
        desc += f"Nalezená klíčová slova: {kw_str}"
        SubElement(item, "description").text = desc

        try:
            dt = datetime.strptime(d["date"], "%d.%m.%Y").replace(tzinfo=timezone.utc)
            SubElement(item, "pubDate").text = dt.strftime(
                "%a, %d %b %Y 12:00:00 +0000"
            )
            SubElement(item, "dc:date").text = dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    return rss


def main():
    print("Stahuji úřední desku NS ČR (civilní kolegium)...")
    try:
        all_decisions = fetch_all_decisions()
    except Exception as e:
        print(f"CHYBA při stahování: {e}")
        all_decisions = []
    print(f"Nalezeno {len(all_decisions)} rozhodnutí celkem")

    matched = []
    for d in all_decisions:
        if not d["pdf_url"]:
            print(f"  {d['case_number']} – bez PDF, přeskakuji")
            continue

        print(f"  Kontroluji {d['case_number']}...", end=" ")
        text = extract_pdf_text(d["pdf_url"])
        if not text:
            print("nelze přečíst")
            continue

        found = find_keywords(text)
        if found:
            print(f"NALEZENO: {', '.join(found)}")
            d["found_keywords"] = found
            d["text"] = text  # uschováme pro AI shrnutí (ať PDF nestahujeme znovu)
            matched.append(d)
        else:
            print("nic")

    print(f"\nRozhodnutí s nepřímým účinkem: {len(matched)}")

    # Okno 2 týdny od prvního výskytu + příznak „nové dnes"
    matched = filter_by_first_seen(
        matched, lambda d: d.get("pdf_url") or d["case_number"], STATE_FILE, weeks=2
    )
    print(f"Po okně 2 týdnů: {len(matched)}")

    matched = enrich_summaries(matched)

    rss = build_rss(matched)
    indent(rss, space="  ")

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    tree = ElementTree(rss)
    tree.write(OUTPUT, encoding="unicode", xml_declaration=True)
    print(f"RSS feed zapsán do {OUTPUT}")


if __name__ == "__main__":
    main()
