#!/usr/bin/env python3
"""Scraper for legal journals – generates RSS feed for new journal issues and articles."""

import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin
from xml.etree.ElementTree import Element, SubElement, ElementTree, fromstring, indent

import requests
from bs4 import BeautifulSoup

from feed_common import (
    JOURNAL_ARTICLE_PROMPT,
    JOURNAL_ISSUE_PROMPT,
    filter_by_first_seen,
    gemini_enabled,
    gemini_summarize_pdf,
    gemini_summarize_text,
    load_json,
    prune_meta,
    save_json,
)

OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "journals_feed.xml")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "journals_seen.json")
# Cache AI shrnutí podle guid ({guid: {"summary": ..., "tag": ...}}).
META_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "journals_meta.json")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# --- ÚPV journals (scrape HTML) ---

def scrape_upv():
    """Scrape ÚPV journal page for latest PDF issues."""
    url = "https://upv.gov.cz/informacni-zdroje/publikace/casopis-dusevni-vlastnictvi"
    base_url = "https://upv.gov.cz"

    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    items = []
    current_year = None

    for el in soup.find_all(["h3", "a"]):
        if el.name == "h3":
            year_match = re.match(r"(\d{4})", el.get_text(strip=True))
            if year_match:
                current_year = year_match.group(1)
            continue

        href = el.get("href", "")
        if not href.endswith(".pdf"):
            continue

        title = el.get_text(strip=True)
        if not title:
            continue

        if href.startswith("/"):
            pdf_url = base_url + href
        elif href.startswith("http"):
            pdf_url = href
        else:
            continue

        issue_match = re.search(r"(\d{4})-?(\d+)", href)
        year = current_year or (issue_match.group(1) if issue_match else None)
        issue_num = issue_match.group(2) if issue_match else ""

        if not year:
            continue

        if "evropske_pravo" in href.lower():
            journal_name, label = "Evropské právo", "EP"
        else:
            journal_name, label = "Duševní vlastnictví", "DV"

        quarter_months = {"1": "01", "01": "01", "2": "04", "02": "04",
                          "3": "07", "03": "07", "4": "10", "04": "10"}
        month = quarter_months.get(issue_num, "01")
        pub_date = datetime.strptime(f"{year}-{month}-01", "%Y-%m-%d").replace(tzinfo=timezone.utc)

        items.append({
            # Prefix [DV]/[EP] zobrazuje stránka jako štítek – jednotně
            # se štítky článků ([RPT], [MUJLT], [GRUR Int], …).
            "title": f"[{label}] {journal_name} {issue_num}/{year}",
            "journal_name": journal_name,
            "link": pdf_url,
            "description": f"{journal_name} {issue_num}/{year}\nPDF: {pdf_url}",
            "guid": f"{journal_name}-{issue_num}-{year}",
            "pub_date": pub_date,
            "sort_key": (year, issue_num),
            "ai_source": "issue",  # shrnutí se dělá z celého PDF čísla
        })

    # Keep only the latest issue per journal name
    items.sort(key=lambda x: x["sort_key"], reverse=True)
    seen = set()
    latest = []
    for item in items:
        if item["journal_name"] not in seen:
            seen.add(item["journal_name"])
            latest.append(item)
    return latest


# --- Časopisy bez použitelného feedu – čteme obsah rovnou z webu ---
# Zdroj většinou nabízí víc adres, kde obsah aktuálního čísla být může.
# Bereme první, která nějaké články vydá, ať se natvrdo nedrží jedna cesta.

def first_page_with_items(page_urls, extract, label):
    """Zkouší adresy po řadě a vrátí články z první, která nějaké má."""
    for url in page_urls:
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"    [diag] {label}: {url} nedostupné ({e})")
            continue
        items = extract(BeautifulSoup(resp.text, "html.parser"), url)
        if items:
            return items
        print(f"    [diag] {label}: {url} bez článků, zkouším další adresu")
    return []


# --- Právník (ÚSP AV ČR) – vlastní web, bez RSS i bez API ---
# Web ÚSP linkuje každý článek přes stabilní ID:
#   …/casopis-pravnik/hledat-v-archivu/detail-clanku.html?id=<id>
# Držíme se tedy tvaru odkazu, ne značek šablony – ta se mění častěji.
# Datum vydání se z výpisu spolehlivě vyčíst nedá; co je nové rozhoduje
# filter_by_first_seen podle guid, takže pub_date stačí orientační.

PRAVNIK_URL = "https://www.ilaw.cas.cz/casopisy-a-knihy/casopisy/casopis-pravnik/"
# Záloha, kdyby titulní stránka obsah aktuálního čísla nevypisovala.
PRAVNIK_ARCHIVE_URL = PRAVNIK_URL + "archiv/"
PRAVNIK_DETAIL_RE = re.compile(r"detail-clanku\.html\?.*\bid=(\d+)", re.IGNORECASE)
PRAVNIK_MAX_ITEMS = 12  # ať první běh nezaplaví feed celým archivem


def _pravnik_articles(soup, page_url):
    """Posbírá odkazy na detaily článků Právníka z jedné stránky webu ÚSP."""
    items = []
    seen_ids = set()
    for a in soup.find_all("a", href=True):
        match = PRAVNIK_DETAIL_RE.search(a["href"])
        if not match:
            continue
        art_id = match.group(1)
        if art_id in seen_ids:
            continue

        title = " ".join(a.get_text(" ", strip=True).split())
        if not title:
            continue
        seen_ids.add(art_id)

        # Odkazy na webu nesou dlouhý ocas z vyhledávacího formuláře
        # (&r=…&query=…) – necháme jen cestu a id.
        detail_path = urljoin(page_url, a["href"]).split("?")[0]
        link = f"{detail_path}?id={art_id}"

        items.append({
            "title": f"[Právník] {title}",
            "journal_name": "Právník",
            "link": link,
            "description": f"{title}\nPrávník (ÚSP AV ČR)",
            "guid": f"Pravnik-{art_id}",
            "pub_date": datetime.now(timezone.utc),
            "sort_key": int(art_id),
            # Anotaci má až detail článku – stáhne se lazy, jen když se
            # pro položku opravdu generuje shrnutí (viz enrich_summaries).
            "ai_source": "page",
        })

    items.sort(key=lambda x: x["sort_key"], reverse=True)
    return items[:PRAVNIK_MAX_ITEMS]


def scrape_pravnik():
    """Vrátí články Právníka z titulní stránky, jinak z archivu."""
    return first_page_with_items(
        [PRAVNIK_URL, PRAVNIK_ARCHIVE_URL], _pravnik_articles, "Právník"
    )


# --- The Lawyer Quarterly (ÚSP AV ČR) – OJS, ale s nepoužitelným RSS ---
# Gateway plugin tady vrací 30 článků seřazených podle interního id, ne podle
# data vydání. Archiv byl někdy přeimportovaný, takže nejvyšší id nesou čísla
# z roku 2021 (id 725–761), zatímco ročník 2026 má id kolem 690 – feed proto
# donekonečna nabízí deset let staré články. Čteme tedy rovnou obsah
# aktuálního čísla; adresu čísla nikde nedržíme natvrdo, ať nezestárne.

TLQ_PAGES = [
    "https://tlq.ilaw.cas.cz/issue/current",
    "https://tlq.ilaw.cas.cz/",
    "https://tlq.ilaw.cas.cz/index.php/tlq/issue/current",
]
# Odkaz na článek, ne na PDF – to má za id ještě číslo galeje.
TLQ_ARTICLE_RE = re.compile(r"/article/view/(\d+)/?$")
TLQ_ISSUE_RE = re.compile(r"Vol\.\s*\d+\s*No\.\s*\d+\s*\(\d{4}\)")
TLQ_GALLEY_LABELS = {"PDF", "HTML", "XML", "EPUB", "FULL TEXT"}
TLQ_MAX_ITEMS = 25


def _tlq_articles(soup, page_url):
    """Posbírá články z obsahu aktuálního čísla TLQ."""
    issue_match = TLQ_ISSUE_RE.search(soup.get_text(" ", strip=True))
    issue = issue_match.group(0) if issue_match else ""

    items = []
    seen_ids = set()
    for a in soup.find_all("a", href=True):
        link = urljoin(page_url, a["href"]).split("?")[0].split("#")[0]
        match = TLQ_ARTICLE_RE.search(link)
        if not match:
            continue
        art_id = match.group(1)
        if art_id in seen_ids:
            continue

        title = " ".join(a.get_text(" ", strip=True).split())
        if not title or title.upper() in TLQ_GALLEY_LABELS:
            continue
        seen_ids.add(art_id)

        # Autoři jsou v OJS vedle názvu ve výpisu čísla; když je šablona
        # nemá, prostě je neuvedeme.
        authors = ""
        summary = a.find_parent(class_="obj_article_summary")
        if summary is not None:
            authors_el = summary.find(class_="authors")
            if authors_el is not None:
                authors = " ".join(authors_el.get_text(" ", strip=True).split())

        full_title = f"[TLQ] {title}"
        desc = [title]
        if authors:
            full_title += f" – {authors}"
            desc.append(f"Autor: {authors}")
        desc.append(f"The Lawyer Quarterly {issue}".strip())

        items.append({
            "title": full_title,
            "journal_name": "The Lawyer Quarterly",
            "link": link,
            "description": "\n".join(desc),
            # Stejný tvar guid jako dřív z OJS RSS, ať se články, které už
            # jednou prošly, neoznačí podruhé jako nové.
            "guid": f"TLQ-{link}",
            "pub_date": datetime.now(timezone.utc),
            # Anotaci má až stránka článku – stáhne se lazy (enrich_summaries).
            "ai_source": "page",
        })

    return items[:TLQ_MAX_ITEMS]


def scrape_tlq():
    """Vrátí články z aktuálního čísla The Lawyer Quarterly."""
    return first_page_with_items(TLQ_PAGES, _tlq_articles, "TLQ")


# --- Časopisy na OJS (Open Journal Systems) – čteme jejich RSS gateway ---
# Stejný vzor pro všechny instalace OJS (journals.muni.cz, jipitec.eu):
# <base>/gateway/plugin/WebFeedGatewayPlugin/rss2

OJS_SOURCES = [
    # (feed_url, zkratka do titulku a guid, plný název časopisu)
    ("https://journals.muni.cz/revue/gateway/plugin/WebFeedGatewayPlugin/rss2",
     "RPT", "Revue pro právo a technologie"),
    ("https://journals.muni.cz/mujlt/gateway/plugin/WebFeedGatewayPlugin/rss2",
     "MUJLT", "Masaryk University Journal of Law and Technology"),
    ("https://www.jipitec.eu/jipitec/gateway/plugin/WebFeedGatewayPlugin/rss2",
     "JIPITEC", "JIPITEC – Journal of Intellectual Property, Information "
                "Technology and E-Commerce Law"),
]

# Pojistka: gateway plugin u některých instalací neřadí články podle data
# vydání, ale podle interního id (viz TLQ níž). Co je starší než rok, do
# feedu nových článků nepatří ani omylem.
OJS_MAX_AGE_DAYS = 365


def fetch_ojs_rss(feed_url, label, journal_name):
    """Stáhne RSS z OJS gateway a vrátí nejnovější články."""
    resp = requests.get(feed_url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()

    root = fromstring(resp.content)
    ns = {"dc": "http://purl.org/dc/elements/1.1/"}
    cutoff = datetime.now(timezone.utc) - timedelta(days=OJS_MAX_AGE_DAYS)
    items = []
    too_old = 0

    for item in root.iter("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        desc_el = item.find("description")
        creator_el = item.find("dc:creator", ns)
        pub_date_el = item.find("pubDate")
        guid_el = item.find("guid")

        title = (title_el.text or "").strip() if title_el is not None else ""
        link = (link_el.text or "").strip() if link_el is not None else ""
        desc = (desc_el.text or "").strip() if desc_el is not None else ""
        creator = (creator_el.text or "").strip() if creator_el is not None else ""
        guid = (guid_el.text or "").strip() if guid_el is not None else link

        # Parse date
        pub_date = None
        if pub_date_el is not None and pub_date_el.text:
            try:
                date_str = pub_date_el.text.strip()
                # Handle Czech date format from OJS
                pub_date = datetime.strptime(
                    re.sub(r"^[A-ZÁ-Žá-ž]+,\s*", "", date_str),
                    "%d %b %Y %H:%M:%S %z"
                )
            except ValueError:
                try:
                    pub_date = datetime.strptime(date_str, "%a, %d %b %Y %H:%M:%S %z")
                except ValueError:
                    pub_date = datetime.now(timezone.utc)

        if (pub_date or datetime.now(timezone.utc)) < cutoff:
            too_old += 1
            continue

        # Clean up description (remove HTML). Plnou anotaci si necháme pro AI,
        # do feedu jde zkrácená verze.
        full_desc = BeautifulSoup(desc, "html.parser").get_text().strip()
        clean_desc = full_desc
        if len(clean_desc) > 300:
            clean_desc = clean_desc[:297] + "..."

        full_title = f"[{label}] {title}"
        if creator:
            full_title += f" – {creator}"

        items.append({
            "title": full_title,
            "journal_name": journal_name,
            "link": link,
            "description": f"{title}\nAutor: {creator}\n{clean_desc}" if creator else f"{title}\n{clean_desc}",
            "guid": f"{label}-{guid}",
            "pub_date": pub_date or datetime.now(timezone.utc),
            "ai_source": "article",  # shrnutí se dělá z názvu a anotace
            "ai_text": f"{title}\n\n{full_desc}".strip(),
        })

    if too_old:
        print(f"    [diag] {label}: {too_old} článků starších než "
              f"{OJS_MAX_AGE_DAYS} dní přeskočeno")
    return items


# --- Časopisy přes Crossref API (OUP, Elgar, Springer) ---
# Weby těchto vydavatelů (academic.oup.com, elgaronline.com) sedí za
# Cloudflare bot-ochranou a přímý scraping z GitHub Actions je nespolehlivý.
# Crossref je jejich oficiální metadatové API: bez ochran, s DOI, autory
# i abstrakty. Novinky bereme podle data vzniku DOI (≈ online publikace).

CROSSREF_API = "https://api.crossref.org/journals/{issn}/works"
CROSSREF_LOOKBACK_DAYS = 30  # jak staré DOI záznamy ještě bereme
# Crossref etiketa: identifikuj se v User-Agent
CROSSREF_UA = "pravni-rss-feed/1.0 (+https://rss.davidzavada.cz)"

CROSSREF_JOURNALS = [
    # (online ISSN, zkratka do titulku a guid, plný název časopisu)
    ("2045-9815", "QMJIP", "Queen Mary Journal of Intellectual Property"),
    ("2632-8550", "GRUR Int", "GRUR International"),
    ("1747-1540", "JIPLP", "Journal of Intellectual Property Law & Practice"),
    ("2195-0237", "IIC", "IIC – International Review of Intellectual Property "
                         "and Competition Law"),
]


def _strip_jats(text):
    """Crossref abstrakty jsou JATS XML (<jats:p>…) – vrátí čistý text."""
    if not text:
        return ""
    clean = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return re.sub(r"^\s*Abstract\s*", "", clean, flags=re.IGNORECASE).strip()


def fetch_crossref_journal(issn, label, journal_name):
    """Vrátí nedávné články časopisu z Crossref (řazené podle vzniku DOI)."""
    since = (datetime.now(timezone.utc)
             - timedelta(days=CROSSREF_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    resp = requests.get(
        CROSSREF_API.format(issn=issn),
        params={
            "filter": f"from-created-date:{since}",
            "sort": "created", "order": "desc", "rows": "40",
            "select": "DOI,title,author,abstract,created",
        },
        headers={"User-Agent": CROSSREF_UA},
        timeout=30,
    )
    resp.raise_for_status()
    works = resp.json().get("message", {}).get("items", [])

    items = []
    for w in works:
        doi = (w.get("DOI") or "").strip()
        titles = w.get("title") or []
        title = " ".join(titles[0].split()) if titles else ""
        if not doi or not title:
            continue

        authors = ", ".join(
            " ".join(p for p in (a.get("given"), a.get("family")) if p)
            for a in (w.get("author") or [])
            if a.get("given") or a.get("family")
        )
        abstract = _strip_jats(w.get("abstract"))

        pub_date = datetime.now(timezone.utc)
        created = (w.get("created") or {}).get("date-time")
        if created:
            try:
                pub_date = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                pass

        clean_desc = abstract if len(abstract) <= 300 else abstract[:297] + "..."
        desc_parts = [title]
        if authors:
            desc_parts.append(f"Autor: {authors}")
        desc_parts.append(journal_name)
        if clean_desc:
            desc_parts.append(clean_desc)

        full_title = f"[{label}] {title}"
        if authors:
            full_title += f" – {authors}"

        items.append({
            "title": full_title,
            "journal_name": journal_name,
            "link": f"https://doi.org/{doi}",
            "description": "\n".join(desc_parts),
            "guid": f"{label}-{doi}",
            "pub_date": pub_date,
            "ai_source": "article",  # shrnutí se dělá z názvu a abstraktu
            "ai_text": f"{title}\n\n{abstract}".strip(),
        })

    return items


# --- AI shrnutí (Gemma) ---

def fetch_page_text(url, limit=6000):
    """Čitelný text stránky pro AI shrnutí (bez navigace, skriptů a patičky)."""
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for junk in soup(["script", "style", "nav", "header", "footer", "form"]):
        junk.decompose()
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
    return text[:limit]

def enrich_summaries(items):
    """Doplní AI shrnutí (HESLO + SHRNUTÍ) přes Gemma; cache podle guid.

    Volá se až na ponechané položky (po okně). Články (OJS, Crossref)
    shrnuje z názvu a anotace/abstraktu, čísla časopisů (ÚPV) z celého PDF.
    """
    meta = load_json(META_FILE)
    if not gemini_enabled():
        for it in items:
            m = meta.get(it["guid"], {})
            it["summary"] = m.get("summary", "")
            it["tag"] = m.get("tag", "")
        return items

    summarized = 0
    calls = 0
    for it in items:
        g = it["guid"]
        m = meta.get(g, {})
        if not m.get("summary"):
            summary, tag = "", ""
            if it.get("ai_source") == "article" and it.get("ai_text"):
                tlen = len(it["ai_text"])
                print(f"    [diag] {g}: článek, ai_text {tlen} znaků")
                calls += 1
                summary, tag = gemini_summarize_text(it["ai_text"], JOURNAL_ARTICLE_PROMPT)
            elif it.get("ai_source") == "page" and it.get("link"):
                try:
                    text = fetch_page_text(it["link"])
                    print(f"    [diag] {g}: stránka článku, {len(text)} znaků")
                    calls += 1
                    summary, tag = gemini_summarize_text(text, JOURNAL_ARTICLE_PROMPT)
                except Exception as e:
                    print(f"  CHYBA stahování stránky {it['title']}: {e}")
            elif it.get("ai_source") == "issue" and it.get("link"):
                try:
                    pr = requests.get(it["link"], headers={"User-Agent": USER_AGENT}, timeout=120)
                    pr.raise_for_status()
                    print(f"    [diag] {g}: číslo, PDF {len(pr.content)} B")
                    calls += 1
                    summary, tag = gemini_summarize_pdf(pr.content, JOURNAL_ISSUE_PROMPT)
                except Exception as e:
                    print(f"  CHYBA stahování PDF {it['title']}: {e}")
            else:
                print(f"    [diag] {g}: PŘESKAKUJI (source={it.get('ai_source')}, "
                      f"ai_text={bool(it.get('ai_text'))}, link={bool(it.get('link'))})")
            if summary:
                m = {"summary": summary, "tag": tag}
                meta[g] = m
                summarized += 1
            else:
                print(f"    [diag] {g}: bez shrnutí")
        it["summary"] = m.get("summary", "")
        it["tag"] = m.get("tag", "")

    save_json(META_FILE, meta)
    if calls:
        print(f"  AI: {summarized}/{calls} shrnutí vygenerováno")
    return items


# --- Build combined RSS ---

def build_rss(all_items):
    """Build RSS 2.0 XML from all journal items."""
    rss = Element("rss", version="2.0", attrib={
        "xmlns:dc": "http://purl.org/dc/elements/1.1/",
        "xmlns:atom": "http://www.w3.org/2005/Atom",
    })
    channel = SubElement(rss, "channel")

    SubElement(channel, "title").text = "Právní časopisy"
    # <link> kanálu má vést na web, ne na XML samotné (to patří do atom:link self)
    SubElement(channel, "link").text = "https://rss.davidzavada.cz/"
    SubElement(channel, "atom:link", attrib={
        "href": "https://rss.davidzavada.cz/journals_feed.xml",
        "rel": "self", "type": "application/rss+xml",
    })
    SubElement(channel, "description").text = "Nová čísla právních časopisů a články"
    SubElement(channel, "language").text = "cs"
    SubElement(channel, "lastBuildDate").text = datetime.now(timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )

    for item in all_items:
        el = SubElement(channel, "item")
        SubElement(el, "title").text = item["title"]
        SubElement(el, "link").text = item["link"]
        SubElement(el, "guid", isPermaLink="false").text = item["guid"]
        desc = item["description"]
        if item.get("summary"):
            desc = f"{desc}\n{item['summary']}"
        SubElement(el, "description").text = desc
        if item.get("is_new"):
            SubElement(el, "is-new").text = "true"
        # AI shrnutí + heslo (čte je index.html do samostatných sloupců)
        if item.get("tag"):
            SubElement(el, "ai-tag").text = item["tag"]
        if item.get("summary"):
            SubElement(el, "ai-summary").text = item["summary"]
        SubElement(el, "pubDate").text = item["pub_date"].strftime(
            "%a, %d %b %Y 12:00:00 +0000"
        )
        SubElement(el, "dc:date").text = item["pub_date"].strftime("%Y-%m-%d")

    return rss


def main():
    print("Stahuji právní časopisy...")
    all_items = []

    # 1. ÚPV (HTML + PDF čísla)
    print("  Zdroj: Duševní vlastnictví / Evropské právo (ÚPV)")
    try:
        upv = scrape_upv()
        print(f"  Nalezeno {len(upv)} nejnovějších čísel")
        all_items.extend(upv)
    except Exception as e:
        print(f"  CHYBA při stahování ÚPV: {e}")

    # 2. Právník (HTML web ÚSP AV ČR)
    print("  Zdroj: Právník (ÚSP AV ČR)")
    try:
        pravnik = scrape_pravnik()
        print(f"  Nalezeno {len(pravnik)} článků")
        all_items.extend(pravnik)
    except Exception as e:
        print(f"  CHYBA při stahování Právníka: {e}")

    # 3. The Lawyer Quarterly (OJS, ale obsah čteme z aktuálního čísla)
    print("  Zdroj: The Lawyer Quarterly (aktuální číslo)")
    try:
        tlq = scrape_tlq()
        print(f"  Nalezeno {len(tlq)} článků")
        all_items.extend(tlq)
    except Exception as e:
        print(f"  CHYBA při stahování TLQ: {e}")

    # 4. Časopisy na OJS (RPT, MUJLT, JIPITEC)
    for feed_url, label, journal_name in OJS_SOURCES:
        print(f"  Zdroj: {journal_name} (OJS RSS)")
        try:
            ojs_items = fetch_ojs_rss(feed_url, label, journal_name)
            print(f"  Nalezeno {len(ojs_items)} článků")
            all_items.extend(ojs_items)
        except Exception as e:
            print(f"  CHYBA při stahování {label}: {e}")

    # 5. Časopisy přes Crossref (QMJIP, GRUR Int, JIPLP, IIC)
    for issn, label, journal_name in CROSSREF_JOURNALS:
        print(f"  Zdroj: {journal_name} (Crossref)")
        try:
            cr_items = fetch_crossref_journal(issn, label, journal_name)
            print(f"  Nalezeno {len(cr_items)} článků")
            all_items.extend(cr_items)
        except Exception as e:
            print(f"  CHYBA při stahování {label}: {e}")

    # Ponecháme jen položky s prvním výskytem do 2 týdnů zpět (u všech zdrojů).
    # První výskyt sledujeme sami, aby se staré články s přepsaným datem nevracely.
    all_items = filter_by_first_seen(
        all_items, lambda i: i["guid"], STATE_FILE, weeks=2
    )

    # Sort by date desc
    all_items.sort(key=lambda x: x["pub_date"], reverse=True)

    all_items = enrich_summaries(all_items)  # AI shrnutí jen na ponechané

    # Cache shrnutí prořízneme podle stavu prvního výskytu, ať neroste donekonečna
    save_json(META_FILE, prune_meta(load_json(META_FILE), STATE_FILE))

    rss = build_rss(all_items)

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    indent(rss, space="  ")
    tree = ElementTree(rss)
    tree.write(OUTPUT, encoding="unicode", xml_declaration=True)
    print(f"RSS feed zapsán do {OUTPUT}")

    for item in all_items[:6]:
        print(f"  {item['title']}")


if __name__ == "__main__":
    main()
