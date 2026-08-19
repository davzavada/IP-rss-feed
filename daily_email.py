#!/usr/bin/env python3
"""Denní e-mail – co dnes přibylo ve feedech.

Čte hotové feedy z docs/ (běží tedy až po ranním scraperu) a z položek
označených <is-new> složí zprávu se stejným obsahem, jaký má na stránce
„Dnešní shrnutí". Když nic nového nepřibylo, neposílá se nic – prázdný
e-mail každé ráno nikdo nechce.

Odesílá se přes SMTP podle proměnných prostředí:

    MAIL_SMTP_HOST    server (bez něj se jen sestaví náhled a skončí)
    MAIL_SMTP_PORT    465 = SMTPS, cokoli jiného = STARTTLS (výchozí 587)
    MAIL_USERNAME     přihlášení k SMTP
    MAIL_PASSWORD     heslo (u Gmailu heslo aplikace)
    MAIL_FROM         odesílatel (výchozí MAIL_USERNAME)
    MAIL_TO           příjemci oddělení čárkou nebo novým řádkem
    MAIL_UNSUBSCRIBE_URL  odhlašovací odkaz do patičky a hlavičky
    MAIL_DRY_RUN=1    jen sestavit, neodesílat

Adresy příjemců jdou do Bcc, aby je jeden druhému neviděl.
"""

import os
import re
import smtplib
import ssl
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from xml.etree.ElementTree import parse as parse_xml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(BASE_DIR, "docs")
BUILD_DIR = os.path.join(BASE_DIR, "build")
SITE_URL = "https://rss.davidzavada.cz/"

# (štítek zdroje, soubor feedu) – pořadí sekcí ve zprávě.
FEEDS = [
    ("NS 23 Cdo", "feed.xml"),
    ("CJEU", "ipcuria_feed.xml"),
    ("Časopisy", "journals_feed.xml"),
]

# Barvy podle stránky (paleta zinc). E-mail nemá stylopis, všechno je inline.
C_TEXT = "#18181b"
C_MUTED = "#71717a"
C_BORDER = "#e4e4e7"
C_LINK = "#2563eb"
C_BADGE_BG = "#f4f4f5"
C_HESLO_BG = "#eef2ff"
C_HESLO_FG = "#4338ca"


def _text(el, tag):
    """Text podelementu, nebo '' když chybí."""
    child = el.find(tag)
    return (child.text or "").strip() if child is not None and child.text else ""


def _parse_pub_date(value):
    """RFC 822 pubDate -> datetime, nebo None."""
    try:
        return datetime.strptime(value, "%a, %d %b %Y %H:%M:%S %z")
    except ValueError:
        return None


def collect_new():
    """Položky označené <is-new>, po zdrojích a v každém od nejnovější."""
    sections = []
    for label, filename in FEEDS:
        path = os.path.join(DOCS_DIR, filename)
        if not os.path.exists(path):
            print(f"  Feed {filename} chybí, přeskakuji")
            continue
        try:
            root = parse_xml(path).getroot()
        except Exception as e:
            print(f"  CHYBA čtení {filename}: {e}")
            continue

        items = []
        for el in root.iter("item"):
            if el.find("is-new") is None:
                continue
            title = _text(el, "title")
            if not title:
                continue
            # Zkratka časopisu / typ řízení ([TLQ], [Referral], …) drží
            # stránka v hranatých závorkách před názvem – tady zvlášť.
            tag = (re.match(r"^\[([^\]]+)\]", title) or [None, ""])[1]
            items.append({
                "tag": tag,
                "title": re.sub(r"^\[[^\]]+\]\s*", "", title),
                "link": _text(el, "link"),
                "authors": _text(el, "{http://purl.org/dc/elements/1.1/}creator"),
                "heslo": _text(el, "ai-tag"),
                # U žádostí bez zveřejněných otázek je místo shrnutí poznámka.
                "summary": _text(el, "ai-summary") or _text(el, "note"),
                "pub_dt": _parse_pub_date(_text(el, "pubDate")),
            })

        items.sort(key=lambda i: i["pub_dt"] or datetime.min.replace(tzinfo=timezone.utc),
                   reverse=True)
        print(f"  {label}: {len(items)} nových")
        if items:
            sections.append((label, items))
    return sections


def cz_date(dt):
    """19. 8. 2026 – bez závislosti na locale runneru."""
    return f"{dt.day}. {dt.month}. {dt.year}"


def cz_count(n):
    """1 novinka / 2 novinky / 5 novinek."""
    if n == 1:
        return "1 novinka"
    if 2 <= n <= 4:
        return f"{n} novinky"
    return f"{n} novinek"


def esc(value):
    """Text do HTML – ať názvy s & nebo < nerozbijí zprávu."""
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def safe_href(url):
    """Do odkazu pustíme jen http(s)."""
    return esc(url) if re.match(r"^https?://", url or "", re.I) else ""


def render_html(sections, now, unsubscribe_url):
    """Zpráva v HTML – tabulkový layout a inline styly, jak to poštovní
    klienti chtějí; jeden blok na položku se čte líp než široká tabulka."""
    out = [
        '<div style="margin:0;padding:24px 12px;background:#ffffff;">',
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"'
        f' style="max-width:680px;margin:0 auto;width:100%;font-family:'
        f'-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Arial,sans-serif;'
        f'color:{C_TEXT};font-size:15px;line-height:1.5;"><tr><td>',
        f'<p style="margin:0 0 4px;font-size:17px;font-weight:600;">'
        f'Právní RSS – dnešní shrnutí</p>',
        f'<p style="margin:0 0 20px;color:{C_MUTED};font-size:13px;">'
        f'{esc(cz_date(now))}</p>',
    ]

    for label, items in sections:
        out.append(
            f'<p style="margin:22px 0 8px;font-size:15px;font-weight:600;'
            f'border-bottom:1px solid {C_BORDER};padding-bottom:6px;">{esc(label)}</p>'
        )
        for item in items:
            out.append('<div style="margin:0 0 18px;">')

            head = []
            if item["tag"]:
                head.append(
                    f'<span style="display:inline-block;background:{C_BADGE_BG};'
                    f'color:{C_MUTED};border-radius:5px;padding:1px 7px;font-size:12px;'
                    f'margin-right:6px;">{esc(item["tag"])}</span>'
                )
            href = safe_href(item["link"])
            title = esc(item["title"])
            head.append(
                f'<a href="{href}" style="color:{C_LINK};text-decoration:none;'
                f'font-weight:600;">{title}</a>' if href
                else f'<span style="font-weight:600;">{title}</span>'
            )
            out.append(f'<div style="margin:0 0 4px;">{"".join(head)}</div>')

            meta = []
            if item["authors"]:
                meta.append(esc(item["authors"]))
            if item["pub_dt"]:
                meta.append(esc(cz_date(item["pub_dt"])))
            if meta:
                out.append(
                    f'<div style="margin:0 0 4px;color:{C_MUTED};font-size:13px;">'
                    f'{" · ".join(meta)}</div>'
                )

            if item["heslo"]:
                out.append(
                    f'<div style="margin:0 0 4px;"><span style="display:inline-block;'
                    f'background:{C_HESLO_BG};color:{C_HESLO_FG};border-radius:5px;'
                    f'padding:1px 7px;font-size:12px;">{esc(item["heslo"])}</span></div>'
                )
            if item["summary"]:
                out.append(
                    f'<div style="margin:0;font-size:14px;">{esc(item["summary"])}</div>'
                )
            out.append("</div>")

    out.append(
        f'<p style="margin:28px 0 0;padding-top:12px;border-top:1px solid {C_BORDER};'
        f'color:{C_MUTED};font-size:12px;">'
        f'<a href="{SITE_URL}" style="color:{C_MUTED};">{esc(SITE_URL)}</a>'
    )
    if safe_href(unsubscribe_url):
        out.append(
            f' &middot; <a href="{safe_href(unsubscribe_url)}" '
            f'style="color:{C_MUTED};">odhlásit odběr</a>'
        )
    out.append("</p></td></tr></table></div>")
    return "\n".join(out)


def render_text(sections, now, unsubscribe_url):
    """Prostá textová verze – povinná alternativa k HTML."""
    lines = [f"Právní RSS – dnešní shrnutí ({cz_date(now)})", ""]
    for label, items in sections:
        lines.append(label.upper())
        lines.append("-" * len(label))
        for item in items:
            head = f"[{item['tag']}] {item['title']}" if item["tag"] else item["title"]
            lines.append(head)
            meta = [m for m in (item["authors"],
                                cz_date(item["pub_dt"]) if item["pub_dt"] else "") if m]
            if meta:
                lines.append("  " + " · ".join(meta))
            if item["heslo"]:
                lines.append(f"  Heslo: {item['heslo']}")
            if item["summary"]:
                lines.append(f"  {item['summary']}")
            if item["link"]:
                lines.append(f"  {item['link']}")
            lines.append("")
        lines.append("")
    lines.append(SITE_URL)
    if unsubscribe_url:
        lines.append(f"Odhlášení: {unsubscribe_url}")
    return "\n".join(lines)


def recipients():
    """Adresy z MAIL_TO – oddělené čárkou, středníkem nebo novým řádkem."""
    return [a for a in re.split(r"[,;\s]+", os.environ.get("MAIL_TO", "")) if a]


def send(subject, html, text, to_addrs, unsubscribe_url):
    """Pošle zprávu přes SMTP. Adresáti jsou v Bcc, To nese odesílatele."""
    host = os.environ.get("MAIL_SMTP_HOST", "").strip()
    # Nenastavený secret přijde jako prázdný řetězec, ne jako chybějící klíč.
    port = int(os.environ.get("MAIL_SMTP_PORT") or 587)
    username = os.environ.get("MAIL_USERNAME", "").strip()
    password = os.environ.get("MAIL_PASSWORD", "")
    sender = os.environ.get("MAIL_FROM", "").strip() or username

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("Právní RSS", sender))
    msg["To"] = sender
    msg["Bcc"] = ", ".join(to_addrs)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    if unsubscribe_url:
        msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    context = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=60, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=60)
        server.starttls(context=context)
    with server:
        if username:
            server.login(username, password)
        server.send_message(msg, from_addr=sender, to_addrs=[sender] + to_addrs)


def main():
    print("Sestavuji denní e-mail...")
    sections = collect_new()
    total = sum(len(items) for _, items in sections)
    now = datetime.now(timezone.utc)

    if not total:
        print("Dnes nic nového – e-mail se neposílá")
        return

    unsubscribe_url = os.environ.get("MAIL_UNSUBSCRIBE_URL", "").strip()
    subject = f"Právní RSS – {cz_date(now)} ({cz_count(total)})"
    html = render_html(sections, now, unsubscribe_url)
    text = render_text(sections, now, unsubscribe_url)

    # Náhled se hodí na doladění vzhledu i na ruční kontrolu z Actions.
    os.makedirs(BUILD_DIR, exist_ok=True)
    with open(os.path.join(BUILD_DIR, "daily_email.html"), "w", encoding="utf-8") as f:
        f.write(html)
    with open(os.path.join(BUILD_DIR, "daily_email.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    print(f"Náhled zapsán do {BUILD_DIR}/daily_email.html")

    if os.environ.get("MAIL_DRY_RUN", "").strip().lower() in ("1", "true", "yes"):
        print(f"MAIL_DRY_RUN – {subject!r} se neodesílá")
        return
    if not os.environ.get("MAIL_SMTP_HOST", "").strip():
        print("MAIL_SMTP_HOST není nastavený – e-mail se neodesílá")
        return

    to_addrs = recipients()
    if not to_addrs:
        print("MAIL_TO je prázdné – není komu poslat")
        return

    try:
        send(subject, html, text, to_addrs, unsubscribe_url)
    except Exception as e:
        print(f"CHYBA odesílání: {e}")
        sys.exit(1)
    print(f"Odesláno {len(to_addrs)} příjemcům: {subject}")


if __name__ == "__main__":
    main()
