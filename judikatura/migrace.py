"""Jednorázová migrace: rozhodnutí senátu 23 Cdo ze starého feedu do archivu.

Starý scraper (scraper.py) držel jen okno dvou týdnů; historie je ale ve
všech verzích docs/feed.xml v gitu. Migrace z nich poskládá každé rozhodnutí,
které kdy ve feedu bylo (spisová značka, odkazy, AI heslo a shrnutí, úřední
heslo), doplní data a první výskyt z feed_meta.json a feed_seen.json
a zapíše je do archivu data/judikatura/ns/. Oblasti a příznak procesní
doplní AI dávkově nad hotovými shrnutími (bez --bez-ai).

Potřebuje celou historii repa (git fetch --unshallow); feed i feed_*.json
čte z historie, i když už jsou smazané. Opakované spuštění jen doplní, co
ve feedu přibylo (hotové oblasti se znovu neklasifikují):
    python -m judikatura.migrace            # s AI klasifikací
    python -m judikatura.migrace --bez-ai
"""

import argparse
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feed_common as fc
from judikatura import analyza, model
from judikatura.sklad import Sklad
from judikatura.soudy import ns
from judikatura.taxonomie import Taxonomie

KOREN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEED = "docs/feed.xml"
DAVKA = 15
UNID_RE = re.compile(r"^[0-9A-F]{32}$", re.I)


def _git(*args):
    return subprocess.run(["git", *args], cwd=KOREN, check=True, capture_output=True,
                          text=True).stdout


def verze_feedu():
    """(čas commitu, XML) pro každou verzi feedu, od nejstarší."""
    for radek in _git("log", "--reverse", "--format=%H %cI", "--", FEED).splitlines():
        h, _, kdy = radek.partition(" ")
        try:
            yield kdy, _git("show", f"{h}:{FEED}")
        except subprocess.CalledProcessError:
            continue


def posledni_json(cesta):
    """JSON ze stromu, nebo z poslední verze v historii (po smazání souboru)."""
    plna = os.path.join(KOREN, cesta)
    if os.path.exists(plna):
        return fc.load_json(plna)
    for h in _git("log", "--format=%H", "--", cesta).split():
        try:
            return json.loads(_git("show", f"{h}:{cesta}"))
        except (subprocess.CalledProcessError, ValueError):
            continue
    return {}


def _t(el, tag):
    x = el.find(tag)
    return (x.text or "").strip() if x is not None and x.text else ""


def polozky(xml):
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return
    for it in root.iter("item"):
        yield {k: _t(it, k) for k in ("title", "link", "guid", "document-url", "category",
                                      "ai-summary", "ai-tag", "pubDate", "description")}


def _datum_z_pubdate(s):
    try:
        return parsedate_to_datetime(s).date().isoformat()
    except (TypeError, ValueError):
        return ""


def posbirej():
    """guid -> nejnovější hodnoty a čas prvního výskytu ve feedu."""
    vse = {}
    for kdy, xml in verze_feedu():
        for p in polozky(xml):
            guid = p["guid"] or p["title"]
            if not guid:
                continue
            zaznam = vse.setdefault(guid, {"prvni": kdy})
            for k, v in p.items():
                if v:
                    zaznam[k] = v
    return vse


def _nejdriv(*casy):
    """Nejstarší z časů, které jdou přečíst (ISO v UTC)."""
    dt = [d for d in (model.z_iso(c) for c in casy) if d]
    return model.iso(min(dt) if dt else model.ted())


def _hledej(vzor, text):
    m = re.search(vzor, text or "")
    return m.group(1) if m else ""


def sestav(vse, meta, seen, tax):
    """Záznamy archivu z posbíraných položek (bez oblastí – ty doplní AI).

    První výskyt je nejstarší z feed_seen.json a prvního commitu, kde se
    položka ve feedu objevila (feed_seen.json se v červnu zakládal znovu).
    Úřední deska vs. databáze se řeší jako v živém běhu: záznam z desky
    zůstane v archivu jako nahrazený a databáze od něj převezme první výskyt."""
    zaznamy = {}
    for guid, p in vse.items():
        titulek = p.get("title", "")
        spz = model.normalizuj_spz(titulek)
        senat, rejstrik = model.senat_z_spz(spz)
        if senat is None:
            continue
        unid = guid.upper() if UNID_RE.match(guid) else ""
        m = meta.get(unid) or meta.get(titulek) or {}
        popis = p.get("description", "")
        # Deska ohlašuje jen vyhlášené rozsudky (feed u nich psal „Rozhodnutí").
        druh = (m.get("typ") or _hledej(r"\((Usnesení|Rozsudek)\b", popis)).lower() if unid \
            else "rozsudek"
        rozhodnuto = (m.get("decided") or _hledej(r"rozhodnuto (\d{4}-\d{2}-\d{2})", popis)
                      or ns.cz_datum(_hledej(r"vyhlášeno ([\d.]+)", popis)))
        prvni = _nejdriv(seen.get(unid), seen.get(titulek), p["prvni"])
        id_ = f"ns:{unid}" if unid else "ns:deska:" + "-".join(filter(None, model.spz_klic(spz)))
        z = model.novy_zaznam(
            "ns", id_, spz=spz, url=p.get("link", ""),
            pdf=p.get("document-url", "") or ("" if unid else p.get("link", "")),
            senat=senat, rejstrik=rejstrik, druh=druh,
            datum=rozhodnuto,
            zverejneno=m.get("published") or _datum_z_pubdate(p.get("pubDate")),
            first_seen=prvni,
            meta={k: v for k, v in {
                "heslo_ns": m.get("heslo") or p.get("category", ""),
                "kategorie": _hledej(r"kategorie ([A-E])\b", popis),
                "zdroj": "" if unid else "úřední deska (vyhlášené rozhodnutí)",
            }.items() if v},
        )
        shrnuti = m.get("summary") or p.get("ai-summary", "")
        if shrnuti:
            # Model se u starých shrnutí nedá určit (v červnu se měnil).
            z["ai"] = {"heslo": m.get("tag") or p.get("ai-tag", ""), "shrnuti": shrnuti,
                       "oblasti": [], "procesni": None, "model": "",
                       "pv": analyza.PROMPT_VERZE, "tv": tax.verze, "at": prvni,
                       "zdroj": "migrace"}
        drive = zaznamy.get(id_)
        if drive:   # totéž rozhodnutí pod jiným guid (deska nejdřív bez odkazu)
            z["first_seen"] = min(z["first_seen"], drive["first_seen"])
            for k, v in drive.items():
                if v and not z.get(k):
                    z[k] = v
        zaznamy[id_] = z

    databaze = {(z["spz_klic"], z["cast"]): z for z in zaznamy.values()
                if not z["id"].startswith("ns:deska:")}
    for z in zaznamy.values():
        db = databaze.get((z["spz_klic"], z["cast"])) if z["id"].startswith("ns:deska:") else None
        if db:
            db["first_seen"] = min(db["first_seen"], z["first_seen"])
            if z.get("ai") and not db.get("ai"):
                db["ai"] = z["ai"]
            z["nahrazeno"] = db["id"]
    return list(zaznamy.values())


def klasifikuj(zaznamy, tax):
    """Oblasti a příznak procesní pro migrovaná shrnutí, po dávkách."""
    se_shrnutim = [z for z in zaznamy if z.get("ai") and not z.get("nahrazeno")]
    hotovo = 0
    for i in range(0, len(se_shrnutim), DAVKA):
        davka = se_shrnutim[i:i + DAVKA]
        vysledek = analyza.klasifikuj_davku(davka, tax)
        for z in davka:
            oblasti, procesni = vysledek.get(z["id"], ([], None))
            if oblasti:
                z["ai"]["oblasti"] = oblasti
                z["ai"]["procesni"] = procesni
                hotovo += 1
        print(f"  klasifikace {min(i + DAVKA, len(se_shrnutim))}/{len(se_shrnutim)}")
    return hotovo


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bez-ai", action="store_true", help="bez klasifikace oblastí")
    args = ap.parse_args()

    tax = Taxonomie()
    meta = posledni_json("feed_meta.json")
    seen = posledni_json("feed_seen.json")
    vse = posbirej()
    zaznamy = sestav(vse, meta, seen, tax)
    print(f"Ve starém feedu {len(vse)} položek, do archivu {len(zaznamy)} záznamů "
          f"({sum(1 for z in zaznamy if z.get('nahrazeno'))} z desky nahrazených databází)")

    # Opakovaný běh (feed mezitím přibyl): hotové oblasti se znovu neklasifikují
    # a u starých záznamů se jen doplní, co se změnilo (deska nahrazená databází).
    sklad = Sklad("ns").nacti(datetime.now(timezone.utc), mesicu=12)
    for z in zaznamy:
        stary_ai = (sklad.zaznamy.get(z["id"]) or {}).get("ai") or {}
        if z.get("ai") and stary_ai.get("oblasti"):
            z["ai"].update(oblasti=stary_ai["oblasti"], procesni=stary_ai.get("procesni"))
    if not args.bez_ai and fc.gemini_enabled():
        print(f"Oblasti doplněny u {klasifikuj(zaznamy, tax)} rozhodnutí")

    nove = zmenene = 0
    for z in sorted(zaznamy, key=lambda z: (z["first_seen"], z["id"])):
        stary = sklad.zaznamy.get(z["id"])
        if not sklad.ma(z["id"]):
            sklad.pridej(z)
            nove += 1
        elif stary is not None and (stary.get("nahrazeno"), stary.get("ai")) != \
                (z.get("nahrazeno"), z.get("ai")):
            stary["nahrazeno"], stary["ai"] = z.get("nahrazeno"), z.get("ai")
            sklad.zmeneno(stary)
            zmenene += 1
    sklad.uloz()
    print(f"Do archivu přidáno {nove} záznamů, upraveno {zmenene}")
    if sklad.exportuj(model.ted(), model.OKNA_DNI["ns"]):
        print("Okno pro web aktualizováno")


if __name__ == "__main__":
    main()
