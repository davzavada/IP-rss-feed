"""Jeden běh sběru judikatury: objevit → zapsat → AI → exportovat.

Soudy běží izolovaně: když jeden zdroj spadne, ostatní doběhnou a jeho
selhání se zapíše do data/judikatura/stav.json. Archiv se ukládá po každé
položce AI, takže ani přerušený běh nepřijde o hotová shrnutí.
"""

import json
import os
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import feed_common as fc
from judikatura import analyza, fronta, model
from judikatura.sklad import DATA_DIR, Sklad, zapis_atomicky
from judikatura.taxonomie import Taxonomie

LOOKBACK_DNI = 10     # objevování zpět podle zveřejnění – snese vynechané běhy
BOOTSTRAP_DNI = 7     # první běh soudu: jen týden, ať AI nezahltí stará rozhodnutí
STAV_DNI = 14         # jak dlouho držet denní spotřebu AI ve stav.json
PACIFIK = ZoneInfo("America/Los_Angeles")   # free-tier limity se resetují o půlnoci tady


def zapis_nalezene(sklad, nalezene, nyni, bootstrap, adapter=None):
    """Zapíše objevené záznamy do archivu. Vrací seznam nových.

    Novým záznamům nejdřív doplní metadata z detailu (adapter.doplnit) –
    datum zveřejnění rozhoduje o prvním výskytu, a tedy o tom, jestli se
    rozhodnutí na webu ukáže jako nové. Když detail nejde stáhnout a datum
    zveřejnění neznáme, záznam počká na další běh – jinak by se i staré
    rozhodnutí tvářilo jako nové."""
    nove = []
    for n in nalezene:
        if sklad.ma(n["id"]):
            stary = sklad.zaznamy.get(n["id"])
            if stary is not None and model.sloucit(stary, n):
                sklad.zmeneno(stary)
            continue
        if adapter is not None:
            try:
                adapter.doplnit(n)
            except Exception as e:
                if not n.get("zverejneno"):
                    print(f"    [diag] {n['id']}: detail nedostupný ({type(e).__name__}), "
                          "přidám příště")
                    continue
                print(f"    [diag] {n['id']}: detail nedostupný ({type(e).__name__})")
        n["first_seen"] = model.iso(model.prvni_vyskyt(n.get("zverejneno"), nyni, bootstrap))
        n["bootstrap"] = bootstrap
        if not _prevezmi_predbezne(sklad, n):
            continue
        sklad.pridej(n)
        nove.append(n)
    return nove


def _nahrazuje(uredni, predbezny):
    """Může úřední záznam nahradit předběžný? U SDEU jen oznámení o předběžné
    otázce – rozsudek nebo stanovisko ve stejné věci ne."""
    return uredni["soud"] != "sdeu" or uredni.get("druh") == predbezny.get("druh")


def _prevezmi_predbezne(sklad, n):
    """Jedno rozhodnutí ze dvou zdrojů: předběžný záznam (deska NS, ipcuria)
    a úřední (databáze NS, oznámení v ÚV).

    Úřední záznam převezme od předběžného hotové shrnutí a předběžný se
    označí jako nahrazený. První výskyt převezme u NS vždy (deska a databáze
    se liší o dny); u SDEU jen se shrnutím – oznámení vyjde za měsíce
    a bez shrnutí se má ukázat jako nové, i s otázkami. Předběžný záznam se
    nepřidá, když už úřední existuje. Vrací False, když se `n` nemá
    přidávat."""
    prefix = model.PREDBEZNE_ID.get(n["soud"])
    if not prefix or not n.get("spz_klic"):
        return True
    stejne = [z for z in sklad.podle_klice(n["spz_klic"])
              if z.get("cast", "") == n.get("cast", "") and not z.get("nahrazeno")]
    if n["id"].startswith(prefix):
        return not any(not z["id"].startswith(prefix) and _nahrazuje(z, n) for z in stejne)
    for pred in [z for z in stejne if z["id"].startswith(prefix) and _nahrazuje(n, z)]:
        if n["soud"] == "ns" or (pred.get("ai") or {}).get("shrnuti"):
            n["first_seen"] = min(n["first_seen"], pred["first_seen"])
        if pred.get("ai") and not n.get("ai"):
            n["ai"] = pred["ai"]
        pred["nahrazeno"] = n["id"]
        sklad.zmeneno(pred)
    return True


def _ai_vycerpana():
    """Žádný model už v tomto běhu neodpoví (klíč odmítnut, vše vyřazeno)."""
    if fc._klic_zamitnut:
        return True
    return all(fc._stav(m)["vyrazen"] for m in fc.gemini_modely())


def zpracuj_ai(sklady, adaptery, nyni, tax, rozpocet):
    """AI rozbor položek z fronty v rámci rozpočtu. Vrací počet hotových."""
    hotovo = 0
    for z in fronta.sestav(sklady, nyni, tax):
        if not rozpocet.dalsi() or _ai_vycerpana():
            break
        sklad = sklady[z["soud"]]
        try:
            obsah = adaptery[z["soud"]].text(z) or {}
        except Exception as e:
            print(f"    [diag] {z['id']}: text nedostupný ({type(e).__name__}: {e})")
            obsah = {}
        if not (obsah.get("text") or obsah.get("pdf")):
            fronta.odlozit(z, nyni, "bez-textu")
            sklad.zmeneno(z)
            sklad.uloz()
            continue
        vysledek, pouzity = analyza.analyzuj(z, obsah, tax)
        rozpocet.zapocitej()
        if vysledek:
            z["ai"] = dict(vysledek, model=pouzity, pv=analyza.PROMPT_VERZE, tv=tax.verze,
                           at=model.iso(model.ted()), zdroj=obsah.get("zdroj", ""))
            z["stav"] = {"pokusy": 0, "dalsi_pokus": None, "duvod": None}
            hotovo += 1
        elif _ai_vycerpana():
            # Klíč odmítnut nebo žádný model nezbyl – za to rozhodnutí nemůže,
            # pokus se mu nepočítá. Dál to v tomto běhu nemá smysl.
            print("    AI: v tomto běhu už nic neodpoví, zbytek fronty příště")
            break
        elif fc.ai_pretizena():
            # Přetížené modely (5xx, limity) – za to rozhodnutí taky nemůže,
            # pokus se nepočítá a zkusí se to v dalším běhu.
            print(f"    [diag] {z['id']}: AI přetížená, pokus se nepočítá")
            continue
        else:
            fronta.odlozit(z, nyni, "ai-selhani")
        sklad.zmeneno(z)
        sklad.uloz()
    return hotovo


def zapis_stav(zdravi, nyni, cesta=None):
    """Zdraví soudů a denní spotřeba AI (podle pacifického dne free tieru)."""
    cesta = cesta or os.path.join(DATA_DIR, "stav.json")
    stav = {}
    if os.path.exists(cesta):
        try:
            with open(cesta, encoding="utf-8") as f:
                stav = json.load(f)
        except (ValueError, OSError):
            stav = {}
    stav.setdefault("soudy", {}).update(zdravi)
    den = datetime.now(PACIFIK).date().isoformat()
    dny = stav.setdefault("ai", {})
    dnes = dny.setdefault(den, {})
    for m, sp in fc.ai_spotreba().items():
        cil = dnes.setdefault(m, {"volani": 0, "vstup": 0, "vystup": 0})
        for k in cil:
            cil[k] += sp.get(k, 0)
    hranice = (datetime.now(PACIFIK).date() - timedelta(days=STAV_DNI)).isoformat()
    stav["ai"] = {d: v for d, v in sorted(dny.items()) if d >= hranice}
    stav["aktualizovano"] = model.iso(nyni)
    zapis_atomicky(cesta, json.dumps(stav, ensure_ascii=False, indent=1, sort_keys=True) + "\n")


def beh(adaptery, soudy, nyni=None, max_polozek=60, max_minut=20, stav_cesta=None,
        data_dir=None, web_dir=None):
    """Celý běh pro vybrané soudy. Vrací souhrn (pro výpis a testy)."""
    nyni = nyni or model.ted()
    tax = Taxonomie()
    sklady, zdravi, souhrn = {}, {}, {}
    for soud in soudy:
        kw = {}
        if data_dir:
            kw["data_dir"] = data_dir
        if web_dir:
            kw["web_dir"] = web_dir
        sklad = Sklad(soud, **kw).nacti(nyni)
        sklady[soud] = sklad
        bootstrap = sklad.prazdny()
        od = (nyni - timedelta(days=BOOTSTRAP_DNI if bootstrap else LOOKBACK_DNI)).date()
        print(f"[{soud}] hledám zveřejněné od {od.isoformat()}"
              + (" (náběh)" if bootstrap else ""))
        try:
            nalezene = adaptery[soud].objev(od, nyni.date())
        except Exception as e:
            traceback.print_exc()
            zdravi[soud] = {"objev": model.iso(nyni), "chyba": type(e).__name__}
            continue
        nove = zapis_nalezene(sklad, nalezene, nyni, bootstrap, adaptery[soud])
        sklad.uloz()
        zdravi[soud] = {"objev": model.iso(nyni), "chyba": None,
                        "nalezeno": len(nalezene), "nove": len(nove)}
        souhrn[soud] = {"nalezeno": len(nalezene), "nove": len(nove)}
        print(f"[{soud}] nalezeno {len(nalezene)}, nových {len(nove)}")

    hotovo = 0
    if fc.gemini_enabled() and sklady:
        rozpocet = fronta.Rozpocet(max_polozek, max_minut)
        hotovo = zpracuj_ai(sklady, adaptery, nyni, tax, rozpocet)
        print(f"AI rozborů: {hotovo}")

    for soud, sklad in sklady.items():
        sklad.uloz()
        if sklad.exportuj(nyni, model.OKNA_DNI[soud]):
            print(f"[{soud}] okno pro web aktualizováno")
    zapis_stav(zdravi, nyni, stav_cesta)
    souhrn["ai"] = hotovo
    return souhrn
