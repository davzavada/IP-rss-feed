#!/usr/bin/env python3
"""Scraper dokumentů kolegia děkana PrF UK – RSS feed s nejnovějšími zápisy z KD."""

import os
import re
from datetime import datetime, timezone
from xml.etree.ElementTree import Element, SubElement, ElementTree, indent

import requests
from bs4 import BeautifulSoup

URL = "https://www.prf.cuni.cz/kolegium-dekana/dokumenty"
BASE_URL = "https://www.prf.cuni.cz"
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "fakulta_feed.xml")

# Některé servery PrF UK odmítají požadavky bez běžné hlavičky User-Agent.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

# Kolik nejnovějších zápisů zařadit do feedu.
MAX_ITEMS = 20

# Datum ve tvaru DD.MM.YYYY (i s mezerami kolem teček).
DATE_RE = re.compile(r"(\d{1,2})\s*\.\s*(\d{1,2})\s*\.\s*(\d{4})")


def parse_date(text):
    """Z textu odkazu vytáhne datum jednání (poslední výskyt DD.MM.YYYY).

    Zvládá i rozsahy jako „5-6.9.2011“ nebo „24.-25.6.2010“ – bere koncové
    datum (den, který stojí těsně před měsícem a rokem).
    """
    matches = DATE_RE.findall(text)
    if not matches:
        return None
    day, month, year = matches[-1]
    try:
        return datetime(int(year), int(month), int(day), tzinfo=timezone.utc)
    except ValueError:
        return None


def fetch_zapisy():
    """Stáhne stránku a vrátí zápisy z kolegia děkana seřazené od nejnovějšího."""
    resp = requests.get(URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    main = soup.find("main") or soup
    seen = set()
    zapisy = []
    for a in main.find_all("a", href=True):
        title = re.sub(r"\s+", " ", a.get_text(strip=True))
        # Zajímají nás jen zápisy z jednání kolegia děkana.
        if "zápis" not in title.lower():
            continue

        href = a["href"]
        link = href if href.startswith("http") else BASE_URL + href
        if link in seen:
            continue
        seen.add(link)

        date = parse_date(title)
        zapisy.append({
            "title": title,
            "link": link,
            "date": date,
        })

    # Seřadíme sestupně podle data; položky bez data dáme na konec.
    zapisy.sort(key=lambda z: z["date"] or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True)
    return zapisy[:MAX_ITEMS]


def build_rss(zapisy):
    """Vytvoří RSS 2.0 XML ze zápisů kolegia děkana."""
    rss = Element("rss", version="2.0", attrib={
        "xmlns:dc": "http://purl.org/dc/elements/1.1/"
    })
    channel = SubElement(rss, "channel")

    SubElement(channel, "title").text = "PrF UK – zápisy z kolegia děkana"
    SubElement(channel, "link").text = URL
    SubElement(channel, "description").text = (
        "Nejnovější zápisy z jednání kolegia děkana Právnické fakulty UK"
    )
    SubElement(channel, "language").text = "cs"
    SubElement(channel, "lastBuildDate").text = datetime.now(timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )

    for z in zapisy:
        item = SubElement(channel, "item")
        SubElement(item, "title").text = z["title"]
        SubElement(item, "link").text = z["link"]
        SubElement(item, "guid", isPermaLink="true").text = z["link"]
        SubElement(item, "description").text = z["title"]

        if z["date"]:
            SubElement(item, "pubDate").text = z["date"].strftime(
                "%a, %d %b %Y 12:00:00 +0000"
            )
            SubElement(item, "dc:date").text = z["date"].strftime("%Y-%m-%d")

    return rss


def main():
    print("Stahuji dokumenty kolegia děkana PrF UK...")
    try:
        zapisy = fetch_zapisy()
    except Exception as e:
        print(f"CHYBA při stahování: {e}")
        zapisy = []
    print(f"Nalezeno {len(zapisy)} nejnovějších zápisů")

    for z in zapisy:
        d = z["date"].strftime("%d.%m.%Y") if z["date"] else "bez data"
        print(f"  - {z['title']} ({d})")

    rss = build_rss(zapisy)
    indent(rss, space="  ")

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    tree = ElementTree(rss)
    tree.write(OUTPUT, encoding="unicode", xml_declaration=True)
    print(f"RSS feed zapsán do {OUTPUT}")


if __name__ == "__main__":
    main()
