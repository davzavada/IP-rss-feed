#!/usr/bin/env python3
"""Týdenní newsletter – dvoutýdenní přehled e-mailem.

Posílá to, co je na stránce v sekci „Co se stalo v uplynulých dvou týdnech":
AI přehled napříč feedy, který v pondělí ráno sestaví digest.py do
docs/digest.json. Newsletter běží až po něm, takže jen přečte hotový soubor.

Když je přehled starší, než by měl být (pondělní běh spadl), radši se nic
neposílá – lidem by přišel týden starý text jako novinka.

Nastavení je celé v proměnných prostředí:

    MAIL_SMTP_HOST    server (bez něj se jen sestaví náhled a skončí)
    MAIL_SMTP_PORT    465 = SMTPS, cokoli jiného = STARTTLS (výchozí 587)
    MAIL_USERNAME     přihlášení k SMTP
    MAIL_PASSWORD     heslo (u Gmailu heslo aplikace)
    MAIL_FROM         odesílatel (výchozí MAIL_USERNAME)
    MAIL_TO           příjemci oddělení čárkou nebo novým řádkem
    MAIL_UNSUBSCRIBE_URL  odhlašovací odkaz do patičky a hlavičky
    MAIL_MAX_AGE_DAYS jak starý přehled ještě poslat (výchozí 3)
    MAIL_DRY_RUN=1    jen sestavit náhled, neodesílat

Adresy příjemců jdou do Bcc, aby je jeden druhému neviděl.
"""

import json
import os
import re
import smtplib
import ssl
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DIGEST_FILE = os.path.join(BASE_DIR, "docs", "digest.json")
BUILD_DIR = os.path.join(BASE_DIR, "build")
SITE_URL = "https://rss.davidzavada.cz/"

DEFAULT_MAX_AGE_DAYS = 3

# Barvy podle stránky (paleta zinc). E-mail nemá stylopis, všechno je inline.
C_TEXT = "#18181b"
C_MUTED = "#71717a"
C_BORDER = "#e4e4e7"
C_LINK = "#2563eb"
C_BADGE_BG = "#f4f4f5"


def load_digest():
    """Přečte docs/digest.json, nebo vrátí None."""
    if not os.path.exists(DIGEST_FILE):
        print(f"  {DIGEST_FILE} neexistuje")
        return None
    try:
        with open(DIGEST_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  CHYBA čtení přehledu: {e}")
        return None


def digest_age_days(data, now):
    """Stáří přehledu ve dnech, nebo None, když datum chybí nebo nedává smysl."""
    try:
        generated = datetime.fromisoformat(data.get("generated", ""))
    except ValueError:
        return None
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    return (now - generated).total_seconds() / 86400


def cz_date(value):
    """ISO datum nebo datetime na '19. 8. 2026'. Nesmysl vrátí jako ''."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return ""
    return f"{value.day}. {value.month}. {value.year}" if value else ""


def esc(value):
    """Text do HTML – ať názvy s & nebo < nerozbijí zprávu."""
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def safe_href(url):
    """Do odkazu na zdroj pustíme jen http(s)."""
    return esc(url) if re.match(r"^https?://", url or "", re.I) else ""


def safe_unsubscribe(url):
    """Odhlašovací odkaz smí být i mailto: – u ručně vedeného seznamu je to
    obvyklá cesta a hlavička List-Unsubscribe ho bere stejně jako https."""
    return esc(url) if re.match(r"^(https?://|mailto:)", url or "", re.I) else ""


def period_label(data):
    """„18. 8. 2026 – 1. 9. 2026", nebo '' bez dat."""
    return " – ".join(p for p in (cz_date(data.get("from")), cz_date(data.get("to"))) if p)


def meta_line(data):
    """Patička přehledu – z kolika položek se vybíralo. Období je v záhlaví."""
    return f"vybráno z {data['total']} položek" if data.get("total") else ""


def render_html(data, unsubscribe_url):
    """Zpráva v HTML – tabulkový layout a inline styly, jak to poštovní
    klienti chtějí."""
    out = [
        '<div style="margin:0;padding:24px 12px;background:#ffffff;">',
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"'
        f' style="max-width:680px;margin:0 auto;width:100%;font-family:'
        f'-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Arial,sans-serif;'
        f'color:{C_TEXT};font-size:15px;line-height:1.55;"><tr><td>',
        '<p style="margin:0 0 4px;font-size:17px;font-weight:600;">'
        'Co se stalo v uplynulých dvou týdnech</p>',
    ]
    period = period_label(data)
    if period:
        out.append(f'<p style="margin:0 0 20px;color:{C_MUTED};font-size:13px;">'
                   f'{esc(period)}</p>')

    if data.get("intro"):
        out.append(f'<p style="margin:0 0 20px;">{esc(data["intro"])}</p>')

    for block in data.get("blocks", []):
        out.append(
            f'<p style="margin:22px 0 6px;font-size:15px;font-weight:600;'
            f'border-bottom:1px solid {C_BORDER};padding-bottom:6px;">'
            f'{esc(block.get("title", ""))}</p>'
        )
        out.append(f'<p style="margin:0 0 8px;font-size:14px;">'
                   f'{esc(block.get("text", ""))}</p>')
        for source in block.get("sources", []):
            badge = (f'<span style="display:inline-block;background:{C_BADGE_BG};'
                     f'color:{C_MUTED};border-radius:5px;padding:1px 7px;font-size:12px;'
                     f'margin-right:6px;">{esc(source.get("label", ""))}</span>')
            tag = ""
            if source.get("tag"):
                tag = (f'<span style="display:inline-block;background:{C_BADGE_BG};'
                       f'color:{C_MUTED};border-radius:5px;padding:1px 7px;font-size:12px;'
                       f'margin-right:6px;">{esc(source["tag"])}</span>')
            href = safe_href(source.get("link", ""))
            title = esc(source.get("title", ""))
            body = (f'<a href="{href}" style="color:{C_LINK};text-decoration:none;">{title}</a>'
                    if href else title)
            out.append(f'<div style="margin:0 0 4px;font-size:13px;">{badge}{tag}{body}</div>')

    meta = meta_line(data)
    if meta:
        out.append(f'<p style="margin:22px 0 0;color:{C_MUTED};font-size:12px;">{esc(meta)}</p>')

    out.append(
        f'<p style="margin:20px 0 0;padding-top:12px;border-top:1px solid {C_BORDER};'
        f'color:{C_MUTED};font-size:12px;">'
        f'<a href="{SITE_URL}" style="color:{C_MUTED};">{esc(SITE_URL)}</a>'
    )
    odhlaseni = safe_unsubscribe(unsubscribe_url)
    if odhlaseni:
        out.append(f' &middot; <a href="{odhlaseni}" style="color:{C_MUTED};">'
                   f'odhlásit odběr</a>')
    out.append("</p></td></tr></table></div>")
    return "\n".join(out)


def render_text(data, unsubscribe_url):
    """Prostá textová verze – povinná alternativa k HTML."""
    period = period_label(data)
    lines = ["Co se stalo v uplynulých dvou týdnech"]
    if period:
        lines.append(period)
    lines.append("")
    if data.get("intro"):
        lines += [data["intro"], ""]

    for block in data.get("blocks", []):
        title = block.get("title", "")
        lines += [title, "-" * len(title), block.get("text", "")]
        for source in block.get("sources", []):
            head = " ".join(p for p in (f"[{source.get('label', '')}]",
                                        f"[{source['tag']}]" if source.get("tag") else "",
                                        source.get("title", "")) if p)
            lines.append(f"  {head}")
            if source.get("link"):
                lines.append(f"    {source['link']}")
        lines.append("")

    meta = meta_line(data)
    if meta:
        lines += [meta, ""]
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
    if safe_unsubscribe(unsubscribe_url):
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
    print("Sestavuji newsletter...")
    data = load_digest()
    if not data or not data.get("blocks"):
        print("Přehled není k dispozici nebo je prázdný – neposílá se nic")
        return

    now = datetime.now(timezone.utc)
    max_age = float(os.environ.get("MAIL_MAX_AGE_DAYS") or DEFAULT_MAX_AGE_DAYS)
    age = digest_age_days(data, now)
    if age is None:
        print("Přehled nemá použitelné datum vzniku – neposílá se nic")
        return
    print(f"  přehled z {cz_date(data.get('generated'))}, stáří {age:.1f} dne")
    if age > max_age:
        print(f"Přehled je starší než {max_age} dne – pondělní běh nejspíš spadl, "
              f"neposílá se nic")
        return

    unsubscribe_url = os.environ.get("MAIL_UNSUBSCRIBE_URL", "").strip()
    if unsubscribe_url and not safe_unsubscribe(unsubscribe_url):
        print(f"  MAIL_UNSUBSCRIBE_URL {unsubscribe_url!r} není http(s) ani mailto: "
              f"– vynechávám ho")
        unsubscribe_url = ""
    period = period_label(data)
    subject = f"Právní RSS – přehled {period}" if period else "Právní RSS – přehled"
    html = render_html(data, unsubscribe_url)
    text = render_text(data, unsubscribe_url)

    # Náhled se hodí na doladění vzhledu i na ruční kontrolu z Actions.
    os.makedirs(BUILD_DIR, exist_ok=True)
    for name, body in (("newsletter.html", html), ("newsletter.txt", text)):
        with open(os.path.join(BUILD_DIR, name), "w", encoding="utf-8") as f:
            f.write(body)
    print(f"Náhled zapsán do {BUILD_DIR}/newsletter.html")

    if os.environ.get("MAIL_DRY_RUN", "").strip().lower() in ("1", "true", "yes"):
        print(f"MAIL_DRY_RUN – {subject!r} se neodesílá")
        return
    if not os.environ.get("MAIL_SMTP_HOST", "").strip():
        print("MAIL_SMTP_HOST není nastavený – newsletter se neodesílá")
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
