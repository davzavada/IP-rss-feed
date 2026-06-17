#!/usr/bin/env python3
"""Scraper IPcuria – generuje RSS feed ze 4 kategorií (rulings, referrals, appeals, pending)."""

import os
import re
from datetime import datetime, timezone, timedelta
from xml.etree.ElementTree import Element, SubElement, ElementTree, indent

import requests
from bs4 import BeautifulSoup

from feed_common import filter_by_first_seen

CURIA_BASE = "https://curia.europa.eu/juris/liste.do?num="
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "ipcuria_feed.xml")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ipcuria_seen.json")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

SOURCES = [
    ("https://ipcuria.eu/all_preliminary_rulings.php", "Ruling"),
    ("https://ipcuria.eu/all_referrals.php", "Referral"),
]


def fetch_all():
    """Stáhne všechny 4 stránky a vrátí rozhodnutí z posledních 31 dnů."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=31)
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


def build_rss(decisions):
    """Vytvoří RSS 2.0 XML z rozhodnutí."""
    rss = Element("rss", version="2.0", attrib={
        "xmlns:dc": "http://purl.org/dc/elements/1.1/"
    })
    channel = SubElement(rss, "channel")

    SubElement(channel, "title").text = "CJEU IP case law"
    SubElement(channel, "link").text = "https://curia.europa.eu/"
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

        desc_parts = [
            f"[{d['category']}] {d['date_str']}, {d['case_ref']}",
        ]
        if d["case_name"]:
            desc_parts[0] += f" ({d['case_name']})"
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
    print("Stahuji IPcuria – 4 kategorie...")
    decisions = fetch_all()
    print(f"Nalezeno {len(decisions)} položek z posledního měsíce")

    # Okno 2 týdny od prvního výskytu + příznak „nové dnes"
    decisions = filter_by_first_seen(
        decisions, lambda d: f"{d['category']}-{d['case_ref']}", STATE_FILE, weeks=2
    )
    decisions.sort(key=lambda d: d["date"], reverse=True)
    print(f"Po okně 2 týdnů: {len(decisions)} položek")

    for d in decisions:
        print(f"  [{d['category']}] {d['case_ref']} {d['case_name']} ({d['date_str']})")

    rss = build_rss(decisions)
    indent(rss, space="  ")

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    tree = ElementTree(rss)
    tree.write(OUTPUT, encoding="unicode", xml_declaration=True)
    print(f"RSS feed zapsán do {OUTPUT}")


if __name__ == "__main__":
    main()
