#!/usr/bin/env python3
"""Scraper for legal journals – generates RSS feed for new journal issues and articles."""

import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from collections import Counter
from urllib.parse import urljoin
from xml.etree.ElementTree import Element, SubElement, ElementTree, fromstring, indent

import requests
from bs4 import BeautifulSoup

from feed_common import (
    JOURNAL_ARTICLE_PROMPT,
    JOURNAL_DECISION_PROMPT,
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

# --- Názvy a autoři článků ---
# Každý zdroj píše metadata po svém: OJS (TLQ) vrací názvy verzálkami a
# k autorům lepí roli („(Author)"). Do feedu chceme jednotný tvar – název
# běžnou sazbou a autoři v samostatném poli (v RSS jako <dc:creator>),
# ne přilepení za názvem.

AUTHOR_ROLE_RE = re.compile(
    r"\s*\((?:authors?|autor(?:ka|ky|i|ři)?|corresponding author|editor|"
    r"translator|překladatel(?:ka)?)\)",
    re.IGNORECASE,
)

# Zkratky, které zůstávají verzálkami i v názvu psaném normálně; hodnota je
# tvar, jak se má psát (kvůli ECtHR a spol.). Množné číslo („SMEs") se odvodí.
TITLE_ACRONYMS = {a.upper(): a for a in (
    "EU", "US", "USA", "UK", "UN", "AI", "IP", "ICT", "IoT", "GDPR", "DSA",
    "DMA", "DSM", "TRIPS", "WIPO", "WTO", "CJEU", "ECJ", "ECHR", "ECtHR",
    "CFSP", "TFEU", "TEU", "NATO", "OECD", "PCT", "EPO", "EPC", "UPC", "SPC",
    "SEP", "FRAND", "ISDS", "NGO", "SME", "NFT", "USPTO", "EUIPO", "ÚPV",
    "ČR", "SR", "DNA", "AML", "ADR", "ODR", "ESG", "B2B", "B2C", "COVID",
)}

# Krátká slova, která se v anglickém názvu nepíšou s velkým písmenem
# (pokud nestojí na začátku).
TITLE_LOWER_WORDS = {
    "a", "an", "the", "and", "or", "nor", "but", "as", "at", "by", "for",
    "from", "in", "into", "of", "on", "onto", "over", "per", "to", "under",
    "up", "via", "vs", "with", "within", "without",
}

WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
# Po těchhle znacích začíná další „věta" – tam se malá slova zase píšou velkým.
SENTENCE_END = (":", ".", "?", "!", ";", "–", "—")


def clean_authors(raw):
    """Autoři na jednotný tvar: bez rolí z OJS a bez zdvojených mezer."""
    if not raw:
        return ""
    names = [
        " ".join(AUTHOR_ROLE_RE.sub("", n).split())
        for n in re.split(r"\s*[;,]\s*|\s+and\s+", raw)
    ]
    seen, out = set(), []
    for name in names:
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return ", ".join(out)


def _cased_word(word, sentence_start):
    """Jedno slovo názvu psaného verzálkami převede na běžnou sazbu."""
    def fix(match):
        run = match.group(0)
        before = word[match.start() - 1] if match.start() else ""
        first = sentence_start and match.start() == 0

        acronym = TITLE_ACRONYMS.get(run.upper())
        if acronym:
            return acronym
        # Množné číslo zkratky: SMES -> SMEs, NFTS -> NFTs
        if len(run) > 1 and run.upper().endswith("S"):
            acronym = TITLE_ACRONYMS.get(run[:-1].upper())
            if acronym:
                return acronym + "s"
        # Přípona za apostrofem („CJEU'S") a koncovka za číslicí („3RD")
        if before in ("'", "\u2019") or before.isdigit():
            return run.lower()
        if not first and run.lower() in TITLE_LOWER_WORDS:
            return run.lower()
        return run[0].upper() + run[1:].lower()

    return WORD_RE.sub(fix, word)


def normalize_title(title):
    """Název psaný verzálkami převede na běžnou sazbu, ostatní nechá být.

    Rozhoduje podíl velkých písmen, ne zdroj – ať se to chytne i tam, kde
    verzálky jsou jen občas. Krátké názvy (zkratky, „AI ACT") neřešíme.
    """
    letters = [c for c in title if c.isalpha()]
    if len(letters) < 12 or sum(c.isupper() for c in letters) / len(letters) < 0.8:
        return title

    out, sentence_start = [], True
    for word in title.split(" "):
        out.append(_cased_word(word, sentence_start))
        stripped = word.rstrip("\"'\u201d\u2019)")
        if stripped:
            sentence_start = stripped.endswith(SENTENCE_END)
    return " ".join(out)


def clean_title(raw):
    """Název na čistý text: bez vloženého HTML a bez verzálek.

    Crossref i Wiley posílají v názvech kurzívu (<i>…</i>) a další značky –
    do feedu i do podkladu pro AI patří samotný text.
    """
    text = BeautifulSoup(raw or "", "html.parser").get_text(" ")
    return normalize_title(" ".join(text.split()))


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

def _link_shapes(soup, limit=8):
    """Nejčastější tvary odkazů na stránce – čísla nahrazená N, z dotazu jen
    názvy parametrů. Když scraper nic nenajde, je z toho hned vidět, čím se
    na té stránce vlastně odkazuje."""
    shapes = Counter()
    for a in soup.find_all("a", href=True):
        path, _, query = a["href"].split("#")[0].partition("?")
        shape = re.sub(r"\d+", "N", path)
        if query:
            keys = sorted({p.split("=")[0] for p in query.split("&") if p})
            shape += "?" + "&".join(keys)
        shapes[shape] += 1
    return ", ".join(f"{s} ({n}×)" for s, n in shapes.most_common(limit)) or "(žádné)"


def first_page_with_items(page_urls, extract, label):
    """Zkouší adresy po řadě a vrátí články z první, která nějaké má."""
    for url in page_urls:
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"    [diag] {label}: {url} nedostupné ({e})")
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        items = extract(soup, url)
        if items:
            return items
        print(f"    [diag] {label}: {url} bez článků, zkouším další adresu")
        print(f"    [diag] {label}: odkazy na stránce – {_link_shapes(soup)}")
    return []


# --- Právník (ÚSP AV ČR) – vlastní web, bez RSS i bez API ---
# Titulní stránka vypisuje obsah aktuálního čísla a každý článek na ní vede
# do archivu jako …/archiv/<rok>/<číslo>-<rok>.html?a=<id>. Stránka čísla
# (bez ?a=) je jen obsah, těch je v archivu přes čtyři stovky – bereme proto
# jen odkazy s ?a=, což jsou samotné články.
# Datum vydání se z výpisu spolehlivě vyčíst nedá; co je nové rozhoduje
# filter_by_first_seen podle guid, takže pub_date stačí orientační.

PRAVNIK_BASE = "https://www.ilaw.cas.cz/casopisy-a-knihy/casopisy/casopis-pravnik/"
PRAVNIK_URL = PRAVNIK_BASE
PRAVNIK_ARCHIVE_URL = PRAVNIK_BASE + "archiv/"
PRAVNIK_ARTICLE_RE = re.compile(r"/archiv/(\d{4})/([\d-]+)\.html\?a=(\d+)")
PRAVNIK_MAX_ITEMS = 20  # obsah jednoho čísla, ne celý ročník


def _pravnik_issue_label(slug):
    """Slug čísla (v adrese '2026-8') na obvyklý zápis '8/2026'."""
    match = re.fullmatch(r"(\d{4})-(\d+)", slug)
    if match:
        return f"{match.group(2)}/{match.group(1)}"
    match = re.fullmatch(r"(\d+)-(\d{4})", slug)
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    return slug


def _pravnik_articles(soup, page_url):
    """Posbírá články aktuálního čísla Právníka z titulní stránky ÚSP."""
    # Na jeden článek může vést víc odkazů (název, „detail", ikona) – z textů
    # bereme ten nejdelší, což je název článku.
    found = {}
    for a in soup.find_all("a", href=True):
        link = urljoin(page_url, a["href"])
        match = PRAVNIK_ARTICLE_RE.search(link)
        if not match:
            continue
        year, issue_slug, art_id = match.groups()
        # ?a= je pořadí v čísle, unikátní až s ročníkem a číslem
        guid = f"Pravnik-{year}-{issue_slug}-{art_id}"
        title = " ".join(a.get_text(" ", strip=True).split())
        if len(title) > len(found.get(guid, ("", ""))[0]):
            found[guid] = (title, link, _pravnik_issue_label(issue_slug))

    items = []
    for guid, (title, link, issue) in found.items():
        if not title:
            continue
        items.append({
            "title": f"[Právník] {title}",
            "journal_name": "Právník",
            "link": link,
            "description": f"{title}\nPrávník {issue} (ÚSP AV ČR)",
            "guid": guid,
            "pub_date": datetime.now(timezone.utc),
            "pub_date_odhad": True,
            # Anotaci má až stránka článku – stáhne se lazy, jen když se
            # pro položku opravdu generuje shrnutí (viz enrich_summaries).
            "ai_source": "page",
        })

    return items[:PRAVNIK_MAX_ITEMS]


def scrape_pravnik():
    """Vrátí články aktuálního čísla Právníka."""
    return first_page_with_items(
        [PRAVNIK_URL, PRAVNIK_ARCHIVE_URL], _pravnik_articles, "Právník"
    )


# --- Jurisprudence (jurisprudence.cz) – vlastní web, bez RSS i bez API ---
# Archiv vypisuje ročníky (…/casopis/archiv/<rok>) a jednotlivá čísla
# (…/casopis/archiv/<číslo>-<rok>, např. /archiv/1-2026); stránka čísla je
# obsah a odkazuje na články (…/casopis/<slug>.m-<id>.html). Číslo se hledá
# v archivu, ne natvrdo v adrese – jinak by feed zamrzl na jednom ročníku.
# Kdyby se archiv přestal dát přečíst, radši nevrátíme nic: natvrdo zapsané
# staré číslo by se jednoho dne vysypalo do feedu jako samé novinky.
# Datum vydání se z výpisu vyčíst nedá; co je nové, rozhoduje
# filter_by_first_seen podle guid, takže pub_date stačí orientační.

JURISPRUDENCE_ARCHIVE_URL = "https://www.jurisprudence.cz/cz/casopis/archiv"
JURISPRUDENCE_ISSUE_RE = re.compile(r"/cz/casopis/archiv/(\d+)-(\d{4})/?$")
JURISPRUDENCE_ARTICLE_RE = re.compile(r"/cz/casopis/[^/]+\.m-(\d+)\.html")
JURISPRUDENCE_MAX_ITEMS = 25   # obsah jednoho čísla, ne celý ročník
JURISPRUDENCE_TRY_ISSUES = 2   # nejnovější číslo a jedno předchozí


def _jurisprudence_issue_pages():
    """Adresy, kde archiv vypisuje čísla – rozcestník a poslední dva ročníky.

    Rozcestník u některých šablon vypisuje rovnou čísla, u jiných jen ročníky;
    stránky ročníků jsou tu proto jako druhá cesta ke stejnému seznamu.
    """
    year = datetime.now(timezone.utc).year
    return [
        JURISPRUDENCE_ARCHIVE_URL,
        f"{JURISPRUDENCE_ARCHIVE_URL}/{year}",
        f"{JURISPRUDENCE_ARCHIVE_URL}/{year - 1}",
    ]


def _jurisprudence_issue_urls(soup, page_url):
    """Adresy čísel ze stránky archivu, od nejnovějšího."""
    issues = {}
    for a in soup.find_all("a", href=True):
        link = urljoin(page_url, a["href"]).split("?")[0].split("#")[0]
        match = JURISPRUDENCE_ISSUE_RE.search(link)
        if match:
            issues[(int(match.group(2)), int(match.group(1)))] = link
    return [issues[key] for key in sorted(issues, reverse=True)]


def _jurisprudence_articles(soup, page_url):
    """Posbírá články z obsahu jednoho čísla Jurisprudence.

    Stránka archivu vypisuje celý strom ročníků a k němu rozbalený obsah
    jednoho čísla. Obsah je jediný `ul.plain-list`, a jen v něm se hledá:
    dvě čísla z roku 2013 mají totiž historicky adresu ve tvaru článku
    (`/cz/casopis/jurisprudence-5-2013.m-59.html`), takže hledání podle
    tvaru adresy je vytáhlo mezi články a AI pak místo odborného textu
    popisovala seznam ročníků.

    V obsahu je u každého článku i autor (`p.article-props`) a nad skupinou
    článků rubrika (`h4`) – obojí se bere s sebou.
    """
    issue_match = JURISPRUDENCE_ISSUE_RE.search(page_url)
    issue = f"{issue_match.group(1)}/{issue_match.group(2)}" if issue_match else ""

    items = []
    videne = set()
    for obsah in soup.select("ul.plain-list"):
        rubrika = ""
        for li in obsah.find_all("li"):
            nadpis = li.find("h4")
            if nadpis is not None:
                rubrika = " ".join(nadpis.get_text(" ", strip=True).split())
                continue

            odkaz = li.find("a", href=True)
            if odkaz is None:
                continue
            link = urljoin(page_url, odkaz["href"]).split("?")[0].split("#")[0]
            match = JURISPRUDENCE_ARTICLE_RE.search(link)
            if not match:
                continue
            art_id = match.group(1)
            title = clean_title(odkaz.get_text(" ", strip=True))
            if not title or art_id in videne:
                continue
            videne.add(art_id)

            props = li.find("p", class_="article-props")
            authors = clean_authors(props.get_text(" ", strip=True)) if props else ""

            popis = [title]
            if authors:
                popis.append(f"Autor: {authors}")
            popis.append(f"Jurisprudence {issue}".rstrip())
            if rubrika:
                popis.append(f"Rubrika: {rubrika}")

            items.append({
                "title": f"[Jurisprudence] {title}",
                "journal_name": "Jurisprudence",
                "authors": authors,
                "link": link,
                "description": "\n".join(popis),
                # m-<id> je stabilní přes celý web, číslo do guid netřeba.
                "guid": f"Jurisprudence-{art_id}",
                # Obsah čísla datum vydání neuvádí – doplní se datum, kdy
                # článek ve feedu přibyl (viz pub_date_odhad v main()).
                "pub_date": datetime.now(timezone.utc),
                "pub_date_odhad": True,
                # Anotaci má až stránka článku – stáhne se lazy, jen když se
                # pro položku opravdu generuje shrnutí (viz enrich_summaries).
                "ai_source": "page",
            })

    if not items:
        print("    [diag] Jurisprudence: obsah čísla (ul.plain-list) nenalezen "
              f"na {page_url}")
    return items[:JURISPRUDENCE_MAX_ITEMS]


def scrape_jurisprudence():
    """Vrátí články z nejnovějšího čísla Jurisprudence."""
    issue_urls = first_page_with_items(
        _jurisprudence_issue_pages(), _jurisprudence_issue_urls, "Jurisprudence (archiv)"
    )
    return first_page_with_items(
        issue_urls[:JURISPRUDENCE_TRY_ISSUES], _jurisprudence_articles, "Jurisprudence"
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
        # TLQ sází názvy článků verzálkami – do feedu jdou běžnou sazbou.
        title = clean_title(title)

        # Autoři jsou v OJS vedle názvu ve výpisu čísla; když je šablona
        # nemá, prostě je neuvedeme.
        authors = ""
        summary = a.find_parent(class_="obj_article_summary")
        if summary is not None:
            authors_el = summary.find(class_="authors")
            if authors_el is not None:
                authors = clean_authors(authors_el.get_text(" ", strip=True))

        desc = [title]
        if authors:
            desc.append(f"Autor: {authors}")
        desc.append(f"The Lawyer Quarterly {issue}".strip())

        items.append({
            "title": f"[TLQ] {title}",
            "journal_name": "The Lawyer Quarterly",
            "authors": authors,
            "link": link,
            "description": "\n".join(desc),
            # Stejný tvar guid jako dřív z OJS RSS, ať se články, které už
            # jednou prošly, neoznačí podruhé jako nové.
            "guid": f"TLQ-{link}",
            "pub_date": datetime.now(timezone.utc),
            "pub_date_odhad": True,
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
        creator = clean_authors(creator_el.text if creator_el is not None else "")
        guid = (guid_el.text or "").strip() if guid_el is not None else link
        title = normalize_title(title)

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

        items.append({
            "title": f"[{label}] {title}",
            "journal_name": journal_name,
            "authors": creator,
            "link": link,
            "description": f"{title}\nAutor: {creator}\n{clean_desc}" if creator else f"{title}\n{clean_desc}",
            "guid": f"{label}-{guid}",
            "pub_date": pub_date or datetime.now(timezone.utc),
            "pub_date_odhad": pub_date is None,
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
CROSSREF_LOOKBACK_DAYS = 30  # jak daleko zpět se ptáme
# Ptáme se dvakrát, protože ani jedno datum samo o sobě nestačí:
#   from-created-date  – kdy vznikl DOI záznam. Chytí i staršího „novinku“,
#                        kterou vydavatel deponoval teprve teď.
#   from-pub-date      – kdy článek vyšel. Chytí čísla, jejichž DOI vydavatel
#                        deponoval dopředu (ahead of print) – tak vypadlo
#                        srpnové číslo QMJIP, deponované o měsíce dřív.
# Výsledky se slučují podle DOI, okno zůstává u obou 30 dní, takže se
# nevyhrne archiv.
CROSSREF_FILTRY = ("from-created-date", "from-pub-date")
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


# IIC vedle článků otiskuje i rozhodnutí soudů. Crossref je pozná podle
# podtitulu, ve kterém je soud, datum a spisová značka – „Decision of the
# Federal Court of Justice of Germany (Bundesgerichtshof) 27 January 2026 –
# Case No. KZR 10/25; ECLI:…“. Bez podtitulu z takové položky zbude jen
# přezdívka věci („FRAND Defence III“), ze které se nedá poznat vůbec nic.
ROZHODNUTI_RE = re.compile(
    r"^\s*(decision|judgment|judgement|order|opinion|ruling)\b|case no\.",
    re.IGNORECASE,
)


def _abstract_text(text):
    """Abstrakt na čistý text – Crossref ho dává jako JATS XML (<jats:p>…),
    Wiley jako HTML. Obojí projde stejným parserem, oběma se zahazuje úvodní
    slovo „Abstract“, které v textu není k ničemu."""
    if not text:
        return ""
    clean = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return re.sub(r"^\s*Abstract\s*", "", clean, flags=re.IGNORECASE).strip()


def _crossref_dotaz(issn, filtr, since):
    """Jeden dotaz na Crossref; vrátí položky (list dictů)."""
    resp = requests.get(
        CROSSREF_API.format(issn=issn),
        params={
            "filter": f"{filtr}:{since}",
            "sort": "created", "order": "desc", "rows": "40",
            "select": "DOI,title,subtitle,author,abstract,created,"
                      "published,published-online,issued",
        },
        headers={"User-Agent": CROSSREF_UA},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("message", {}).get("items", [])


def _crossref_datum(w):
    """Datum vydání článku; teprve když chybí, datum vzniku DOI záznamu."""
    for pole in ("published-online", "published", "issued", "created"):
        casti = (w.get(pole) or {}).get("date-parts") or []
        if casti and casti[0] and casti[0][0]:
            r, m, d = (list(casti[0]) + [1, 1])[:3]
            try:
                return datetime(int(r), int(m or 1), int(d or 1), tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def fetch_crossref_journal(issn, label, journal_name):
    """Vrátí nedávné články časopisu z Crossref."""
    since = (datetime.now(timezone.utc)
             - timedelta(days=CROSSREF_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    works, videne = [], set()
    for filtr in CROSSREF_FILTRY:
        try:
            nalezene = _crossref_dotaz(issn, filtr, since)
        except requests.RequestException as e:
            print(f"    [diag] {label}: dotaz {filtr} selhal: {e}")
            continue
        pridano = 0
        for w in nalezene:
            doi = (w.get("DOI") or "").strip().lower()
            if doi and doi not in videne:
                videne.add(doi)
                works.append(w)
                pridano += 1
        print(f"    [diag] {label}: {filtr} vrátil {len(nalezene)}, "
              f"z toho nových {pridano}")

    items = []
    for w in works:
        doi = (w.get("DOI") or "").strip()
        titles = w.get("title") or []
        title = clean_title(titles[0]) if titles else ""
        if not doi or not title:
            continue

        # Podtitul nese u rozhodnutí soud, datum i spisovou značku; u článků
        # druhou půlku názvu. ECLI za středníkem je do názvu už moc dlouhé.
        subtitles = w.get("subtitle") or []
        subtitle = clean_title(subtitles[0]) if subtitles else ""
        je_rozhodnuti = bool(subtitle and ROZHODNUTI_RE.search(subtitle))
        if subtitle:
            title = f"{title} – {subtitle.split(';')[0].strip()}"

        authors = clean_authors("; ".join(
            " ".join(p for p in (a.get("given"), a.get("family")) if p)
            for a in (w.get("author") or [])
            if a.get("given") or a.get("family")
        ))
        abstract = _abstract_text(w.get("abstract"))

        pub_date = _crossref_datum(w) or datetime.now(timezone.utc)

        clean_desc = abstract if len(abstract) <= 300 else abstract[:297] + "..."
        desc_parts = [title]
        if authors:
            desc_parts.append(f"Autor: {authors}")
        desc_parts.append(journal_name)
        if clean_desc:
            desc_parts.append(clean_desc)

        polozka = {
            "title": f"[{label}] {title}",
            "journal_name": journal_name,
            "authors": authors,
            "link": f"https://doi.org/{doi}",
            "description": "\n".join(desc_parts),
            "guid": f"{label}-{doi}",
            "pub_date": pub_date,
            "ai_source": "article",  # shrnutí se dělá z názvu a abstraktu
            "ai_text": f"{title}\n\n{abstract}".strip(),
        }
        if je_rozhodnuti:
            # Rozhodnutí nemá abstrakt, zato má na stránce vydavatele právní
            # věty i odůvodnění – shrnutí se dělá z ní a jiným promptem.
            polozka["ai_source"] = "page"
            polozka["ai_prompt"] = JOURNAL_DECISION_PROMPT
            polozka["ai_text"] = ""
            polozka["ai_fallback_tag"] = "Rozhodnutí"
        items.append(polozka)

    return items


# --- Wiley (JWIP) – vlastní RSS, se zálohou v Crossref ---
# Wiley Online Library nabízí pro každý časopis feed nejnovějších článků
# (/feed/<issn bez pomlčky>/most-recent) včetně abstraktu, který Wiley do
# Crossref často nedeponuje. Feed ale sedí za Cloudflare a z GitHub Actions
# nemusí projít – když se nestáhne nebo je prázdný, sáhneme po Crossref.
# Guid se v obou případech skládá z DOI, takže se článek při přepnutí zdroje
# neoznačí podruhé jako nový.

WILEY_FEED = "https://onlinelibrary.wiley.com/feed/{issn}/most-recent"
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s?&#\"<>]+")
# Feed vypisuje i starší články ročníku – co je starší než rok, do novinek nepatří
WILEY_MAX_AGE_DAYS = 365

JWIP_ISSN = "1747-1796"
JWIP_LABEL = "JWIP"
JWIP_NAME = "The Journal of World Intellectual Property"


def _item_text(item, tag, ns=None):
    """Text potomka <item>, nebo prázdný řetězec."""
    el = item.find(tag, ns) if ns else item.find(tag)
    return (el.text or "").strip() if el is not None and el.text else ""


def _wiley_doi(item, ns):
    """DOI článku – z <prism:doi>, jinak z guid nebo odkazu."""
    doi = _item_text(item, "prism:doi", ns)
    if not doi:
        for field in ("guid", "link"):
            match = DOI_RE.search(_item_text(item, field))
            if match:
                doi = match.group(0)
                break
    return doi.strip().rstrip(".").lower()


def fetch_wiley_rss(issn, label, journal_name):
    """Vrátí nedávné články časopisu z RSS Wiley Online Library."""
    feed_url = WILEY_FEED.format(issn=issn.replace("-", ""))
    resp = requests.get(feed_url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()

    root = fromstring(resp.content)
    ns = {
        "dc": "http://purl.org/dc/elements/1.1/",
        "prism": "http://prismstandard.org/namespaces/basic/2.0/",
    }
    cutoff = datetime.now(timezone.utc) - timedelta(days=WILEY_MAX_AGE_DAYS)
    items = []
    too_old = 0

    for item in root.iter("item"):
        title = clean_title(_item_text(item, "title"))
        if not title:
            continue

        doi = _wiley_doi(item, ns)
        link = f"https://doi.org/{doi}" if doi else _item_text(item, "link")
        if not link:
            continue

        authors = clean_authors(_item_text(item, "dc:creator", ns))
        abstract = _abstract_text(_item_text(item, "description"))

        pub_date = datetime.now(timezone.utc)
        raw_date = _item_text(item, "pubDate")
        if raw_date:
            try:
                pub_date = parsedate_to_datetime(raw_date)
                if pub_date.tzinfo is None:
                    pub_date = pub_date.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass
        if pub_date < cutoff:
            too_old += 1
            continue

        clean_desc = abstract if len(abstract) <= 300 else abstract[:297] + "..."
        desc_parts = [title]
        if authors:
            desc_parts.append(f"Autor: {authors}")
        desc_parts.append(journal_name)
        if clean_desc:
            desc_parts.append(clean_desc)

        items.append({
            "title": f"[{label}] {title}",
            "journal_name": journal_name,
            "authors": authors,
            "link": link,
            "description": "\n".join(desc_parts),
            # Stejný tvar guid jako u Crossref, ať se článek po přepnutí zdroje
            # neoznačí podruhé jako nový.
            "guid": f"{label}-{doi}" if doi else f"{label}-{link}",
            "pub_date": pub_date,
            "ai_source": "article",
            "ai_text": f"{title}\n\n{abstract}".strip(),
        })

    if too_old:
        print(f"    [diag] {label}: {too_old} článků starších než "
              f"{WILEY_MAX_AGE_DAYS} dní přeskočeno")
    return items


def scrape_jwip():
    """Články JWIP – přednostně z RSS Wiley, při potížích z Crossref."""
    try:
        items = fetch_wiley_rss(JWIP_ISSN, JWIP_LABEL, JWIP_NAME)
        if items:
            return items
        reason = "feed bez článků"
    except Exception as e:
        reason = f"feed nedostupný ({e})"
    print(f"    [diag] {JWIP_LABEL}: RSS Wiley – {reason}, beru Crossref")
    return fetch_crossref_journal(JWIP_ISSN, JWIP_LABEL, JWIP_NAME)


# --- AI shrnutí (Gemma) ---

# Vydavatelé místo obsahu občas pošlou hlášku o vypnutém JavaScriptu nebo
# kontrolu prohlížeče. Takový text nesmí jít do AI: model z něj buď udělá
# nesmysl, nebo (jako Gemma u FRAND Defence III) popíše samotnou chybovou
# hlášku – a to se pak uloží do cache jako shrnutí článku.
BLOKACE_RE = re.compile(
    r"javascript is disabled|enable javascript|just a moment|"
    r"checking your (browser|connection)|access denied|are you a robot|"
    r"unusual traffic",
    re.IGNORECASE,
)
MIN_TEXT_PRO_AI = 600      # kratší stránka není článek, ale rozcestník

# Bez hlaviček Accept posílají někteří vydavatelé osekanou verzi stránky.
PAGE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,cs;q=0.8",
}


def page_author(soup):
    """Autor ze stránky článku. Právník ho uvádí jen tady (`div.meta.author`),
    obsah čísla ho nenese – a stránka se stejně stahuje kvůli shrnutí."""
    el = soup.select_one("article .meta.author, .magazineDetail .meta.author")
    return clean_authors(el.get_text(" ", strip=True)) if el else ""


def fetch_page(url, limit=6000):
    """Stránku článku vrátí jako (soup, text pro AI).

    Text je prázdný, když stránka obsah nedala – poslala hlášku o vypnutém
    JavaScriptu, kontrolu prohlížeče, nebo je podezřele krátká. Žádné
    shrnutí je lepší než shrnutí chybové stránky. Soup se vrací i tak,
    metadata (autor) v ní být můžou.
    """
    resp = requests.get(url, headers=PAGE_HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    cely = BeautifulSoup(resp.text, "html.parser")
    for junk in soup(["script", "style", "nav", "header", "footer", "form"]):
        junk.decompose()
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
    if len(text) < MIN_TEXT_PRO_AI or BLOKACE_RE.search(text[:2000]):
        print(f"    [diag] {url}: stránka bez obsahu ({len(text)} znaků): "
              f"{text[:150]!r}")
        return cely, ""
    return cely, text[:limit]


def fetch_page_text(url, limit=6000):
    """Jen text stránky – viz fetch_page()."""
    return fetch_page(url, limit)[1]

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
            it["authors"] = it.get("authors") or m.get("authors", "")
        return items

    summarized = 0
    calls = 0
    for it in items:
        g = it["guid"]
        m = meta.get(g, {})
        # Autora nese u některých zdrojů až stránka článku (Právník). Když
        # ho položka nemá a není ani v cache, stránku si vyžádáme i tehdy,
        # když už shrnutí máme.
        chybi_autor = (it.get("ai_source") == "page" and not it.get("authors")
                       and not m.get("authors"))
        if not m.get("summary") or chybi_autor:
            summary, tag = "", ""
            # Rozhodnutí otištěné v časopise se shrnuje jinak než článek.
            prompt = it.get("ai_prompt") or JOURNAL_ARTICLE_PROMPT
            if it.get("ai_source") == "article" and it.get("ai_text") and not m.get("summary"):
                tlen = len(it["ai_text"])
                print(f"    [diag] {g}: článek, ai_text {tlen} znaků")
                calls += 1
                summary, tag = gemini_summarize_text(it["ai_text"], prompt)
            elif it.get("ai_source") == "page" and it.get("link"):
                try:
                    soup, text = fetch_page(it["link"])
                    if not it.get("authors"):
                        it["authors"] = page_author(soup)
                    if text and not m.get("summary"):
                        print(f"    [diag] {g}: stránka článku, {len(text)} znaků")
                        calls += 1
                        summary, tag = gemini_summarize_text(text, prompt)
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
                m = dict(m, summary=summary, tag=tag)
                meta[g] = m
                summarized += 1
            elif not m.get("summary"):
                print(f"    [diag] {g}: bez shrnutí")
            if it.get("authors"):
                m = dict(m, authors=it["authors"])
                meta[g] = m
        it["summary"] = m.get("summary", "")
        it["tag"] = m.get("tag", "")
        it["authors"] = it.get("authors") or m.get("authors", "")
        # U rozhodnutí je plný text jen na stránce vydavatele, a ta se ne vždy
        # stáhne. Shrnutí se v takovém případě nevymýšlí – aspoň ať sloupec
        # Heslo řekne, že jde o rozhodnutí; soud, datum i značka jsou v názvu.
        if not it["tag"] and it.get("ai_fallback_tag"):
            it["tag"] = it["ai_fallback_tag"]

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
        # Autoři samostatně, ne přilepení za názvem – stránka i čtečky si je
        # zobrazí ve vlastním sloupci.
        if item.get("authors"):
            SubElement(el, "dc:creator").text = item["authors"]
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

    # 3. Jurisprudence (HTML web, nejnovější číslo z archivu)
    print("  Zdroj: Jurisprudence (nejnovější číslo)")
    try:
        jurisprudence = scrape_jurisprudence()
        print(f"  Nalezeno {len(jurisprudence)} článků")
        all_items.extend(jurisprudence)
    except Exception as e:
        print(f"  CHYBA při stahování Jurisprudence: {e}")

    # 4. The Lawyer Quarterly (OJS, ale obsah čteme z aktuálního čísla)
    print("  Zdroj: The Lawyer Quarterly (aktuální číslo)")
    try:
        tlq = scrape_tlq()
        print(f"  Nalezeno {len(tlq)} článků")
        all_items.extend(tlq)
    except Exception as e:
        print(f"  CHYBA při stahování TLQ: {e}")

    # 5. Časopisy na OJS (RPT, MUJLT, JIPITEC)
    for feed_url, label, journal_name in OJS_SOURCES:
        print(f"  Zdroj: {journal_name} (OJS RSS)")
        try:
            ojs_items = fetch_ojs_rss(feed_url, label, journal_name)
            print(f"  Nalezeno {len(ojs_items)} článků")
            all_items.extend(ojs_items)
        except Exception as e:
            print(f"  CHYBA při stahování {label}: {e}")

    # 6. Časopisy přes Crossref (QMJIP, GRUR Int, JIPLP, IIC)
    for issn, label, journal_name in CROSSREF_JOURNALS:
        print(f"  Zdroj: {journal_name} (Crossref)")
        try:
            cr_items = fetch_crossref_journal(issn, label, journal_name)
            print(f"  Nalezeno {len(cr_items)} článků")
            all_items.extend(cr_items)
        except Exception as e:
            print(f"  CHYBA při stahování {label}: {e}")

    # 6. JWIP (RSS Wiley, se zálohou v Crossref)
    print(f"  Zdroj: {JWIP_NAME} (Wiley RSS)")
    try:
        jwip = scrape_jwip()
        print(f"  Nalezeno {len(jwip)} článků")
        all_items.extend(jwip)
    except Exception as e:
        print(f"  CHYBA při stahování {JWIP_LABEL}: {e}")

    # Ponecháme jen položky s prvním výskytem do 2 týdnů zpět (u všech zdrojů).
    # První výskyt sledujeme sami, aby se staré články s přepsaným datem nevracely.
    all_items = filter_by_first_seen(
        all_items, lambda i: i["guid"], STATE_FILE, weeks=4
    )

    # Zdroje, které datum vydání neuvádějí (weby českých časopisů), dostanou
    # datum prvního výskytu. Bez toho by se jim datum při každém běhu
    # přepsalo na „dnes" a ve feedu by vypadaly pořád jako čerstvé.
    for it in all_items:
        if it.pop("pub_date_odhad", False) and it.get("first_seen"):
            it["pub_date"] = it["first_seen"]

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
