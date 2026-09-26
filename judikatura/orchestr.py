"""Jeden běh sběru judikatury: objevit → zapsat → AI → exportovat.

Soudy běží izolovaně: když jeden zdroj spadne, ostatní doběhnou a jeho
selhání se zapíše do data/judikatura/stav.json (a scraper_judikatura.py ho
ohlásí nenulovým kódem). Archiv i okna pro web se ukládají hned po
objevování, archiv pak po každé položce AI a okna znovu na konci (i když
běh přeruší výjimka nebo Ctrl+C), takže přerušený běh nepřijde ani o nová
rozhodnutí, ani o hotová shrnutí.
"""

import json
import os
import time
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
# Kolik minut rozpočtu nechat přepisu hesel, když AI fronta vyčerpá čas
# (nejvýš desetina rozpočtu – krátké ruční běhy ať AI neodříznou).
HESLA_REZERVA_MIN = 5


def zapis_nalezene(sklad, nalezene, nyni, bootstrap, adapter=None, odlozene=None):
    """Zapíše objevené záznamy do archivu. Vrací seznam nových.

    Novým záznamům nejdřív doplní metadata z detailu (adapter.doplnit) –
    datum zveřejnění rozhoduje o prvním výskytu, a tedy o tom, jestli se
    rozhodnutí na webu ukáže jako nové. Když detail nejde stáhnout a datum
    zveřejnění neznáme, záznam počká na další běh (jeho id přibude do
    `odlozene`) – jinak by se i staré rozhodnutí tvářilo jako nové.

    Známým záznamům může adaptér doplnit, co při prvním objevení chybělo
    (`adapter.doplnit_existujici`, např. název věci SDEU, když InfoCuria
    zrovna neodpověděla)."""
    nove = []
    dopln = getattr(adapter, "doplnit_existujici", None)
    for n in nalezene:
        if sklad.ma(n["id"]):
            stary = sklad.zaznamy.get(n["id"])
            if stary is None:
                continue
            zmena = model.sloucit(stary, n)
            if dopln is not None:
                try:
                    zmena = dopln(stary) or zmena
                except Exception as e:
                    print(f"    [diag] {n['id']}: doplnění nevyšlo ({type(e).__name__})")
            if zmena:
                sklad.zmeneno(stary)
            continue
        if adapter is not None:
            try:
                adapter.doplnit(n)
            except Exception as e:
                if not n.get("zverejneno"):
                    print(f"    [diag] {n['id']}: detail nedostupný ({type(e).__name__}), "
                          "přidám příště")
                    if odlozene is not None:
                        odlozene.append(n["id"])
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


def _id_predbezneho(n, prefix):
    """Id předběžného záznamu ke stejné věci: ns:deska:{klíč}[-{část}],
    u SDEU sdeu:ipc:{číslo věci} (s velkým C, jak ho píše ipcuria)."""
    if n["soud"] == "sdeu":
        return prefix + n.get("spz", "")
    return prefix + n["spz_klic"] + (f"-{n['cast']}" if n.get("cast") else "")


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
    if not n["id"].startswith(prefix):
        # Oznámení v ÚV vychází i čtvrt roku po rané otázce z ipcuria – ta
        # pak leží v měsíci, který se běžně nenačítá. Id je dané, dohledá se.
        sklad.dohledej(_id_predbezneho(n, prefix))
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


def _ai_ceka_po_terminu(rozpocet):
    """Všechny modely mají pauzu, která skončí až po konci rozpočtu –
    ai_volani by na ni čekalo a běh by přetáhl limit kroku."""
    pauzy = [fc._stav(m)["pauza_do"] for m in fc.gemini_modely() if not fc._stav(m)["vyrazen"]]
    return bool(pauzy) and min(pauzy) >= rozpocet.konec


def zpracuj_ai(sklady, adaptery, nyni, tax, rozpocet, rezerva=0):
    """AI rozbor položek z fronty v rámci rozpočtu (`rezerva` sekund z něj
    zůstane přepisu hesel). Vrací počet hotových."""
    hotovo = 0
    for z in fronta.sestav(sklady, nyni, tax):
        if not rozpocet.dalsi(rezerva) or _ai_vycerpana():
            break
        if _ai_ceka_po_terminu(rozpocet):
            print("    AI: modely mají pauzu až za konec rozpočtu, zbytek fronty příště")
            break
        sklad = sklady[z["soud"]]
        nazev = z.get("nazev")
        try:
            obsah = adaptery[z["soud"]].text(z) or {}
        except Exception as e:
            print(f"    [diag] {z['id']}: text nedostupný ({type(e).__name__}: {e})")
            obsah = {}
        if z.get("nazev") != nazev:
            sklad.zmeneno(z)   # adaptér doplnil název věci
        if not (obsah.get("text") or obsah.get("pdf")):
            fronta.odlozit(z, nyni, "bez-textu")
            sklad.zmeneno(z)
            sklad.uloz()
            continue
        vysledek, pouzity = analyza.analyzuj(z, obsah, tax)
        rozpocet.zapocitej()
        if vysledek:
            stare = z.get("ai") or {}
            z["ai"] = dict(vysledek, model=pouzity, pv=analyza.PROMPT_VERZE, tv=tax.verze,
                           at=model.iso(model.ted()), zdroj=obsah.get("zdroj", ""))
            # Ručně napsané heslo přežije i nový rozbor.
            if stare.get("heslo_rucne"):
                z["ai"].update(heslo=stare["heslo"], heslo_rucne=True, hv=analyza.HESLO_VERZE)
            # Obecné heslo („Přípustnost dovolání") ještě přepíše prepis_hesel.
            if not analyza.heslo_obecne(vysledek.get("heslo")):
                z["ai"]["hv"] = analyza.HESLO_VERZE
            z["stav"] = {"pokusy": 0, "pokusy_text": 0, "dalsi_pokus": None, "duvod": None}
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


# Kolik dávek hesel (po PREPIS_DAVKA) se za běh přepíše. Stačí to na celé
# okno za dva až tři běhy; nové rozbory mají heslo podle nového pokynu rovnou.
HESLA_MAX_DAVEK = 15
HESLO_POKUSU = 2


def prepis_hesel(sklady, nyni, max_davek=HESLA_MAX_DAVEK, rozpocet=None):
    """Hesla podle starého pokynu (bez `hv`) přepíše ze shrnutí, po dávkách.
    Levné – posílá jen heslo a shrnutí, ne celý text. Končí s koncem
    rozpočtu běhu a když je AI přetížená (další dávky by jen čekaly).
    Vrací počet přepsaných hesel."""
    kandidati = []
    for soud, sklad in sklady.items():
        for z in sklad.v_okne(nyni, model.OKNA_DNI[soud]):
            ai = z.get("ai") or {}
            if (ai.get("shrnuti") and not z.get("nahrazeno") and not ai.get("heslo_rucne")
                    and int(ai.get("hv") or 0) < analyza.HESLO_VERZE):
                kandidati.append(z)
    # Nejdřív ta, co nic neříkají, pak od nejnovějšího.
    kandidati.sort(key=lambda z: (not analyza.heslo_obecne((z.get("ai") or {}).get("heslo")),
                                  analyza_datum(z)), reverse=False)
    prepsano = 0
    for i in range(0, min(len(kandidati), max_davek * analyza.PREPIS_DAVKA), analyza.PREPIS_DAVKA):
        if _ai_vycerpana() or (rozpocet is not None and not rozpocet.cas()):
            break
        davka = kandidati[i:i + analyza.PREPIS_DAVKA]
        nova = analyza.prepis_hesla_davku(davka)
        if nova is None:
            if fc.ai_pretizena():
                print("    AI přetížená – přepis hesel dokončí příští běh")
                break
            continue
        for z in davka:
            ai = z["ai"]
            if z["id"] in nova:
                ai["heslo"] = nova[z["id"]]
                ai["hv"] = analyza.HESLO_VERZE
                ai.pop("hv_pokusy", None)
                prepsano += 1
            else:
                # AI heslo nedala nebo neprošlo (dlouhé, jen procesní) – zkusí
                # se příští běh, nejvýš HESLO_POKUSU×; pak zůstane dosavadní.
                ai["hv_pokusy"] = int(ai.get("hv_pokusy") or 0) + 1
                if ai["hv_pokusy"] >= HESLO_POKUSU:
                    ai["hv"] = analyza.HESLO_VERZE
                    ai.pop("hv_pokusy", None)
            sklady[z["soud"]].zmeneno(z)
        for sklad in sklady.values():
            sklad.uloz()
    if kandidati:
        print(f"Hesla podle nového pokynu: přepsáno {prepsano}, čeká "
              f"{max(0, len(kandidati) - max_davek * analyza.PREPIS_DAVKA)}")
    return prepsano


def analyza_datum(z):
    """Řadicí klíč od nejnovějšího (sestupně přes záporné znaky)."""
    return fronta._zaporne(z.get("zverejneno") or z.get("first_seen") or "")


def nacti_stav(cesta=None):
    cesta = cesta or os.path.join(DATA_DIR, "stav.json")
    if os.path.exists(cesta):
        try:
            with open(cesta, encoding="utf-8") as f:
                return json.load(f)
        except (ValueError, OSError):
            pass
    return {}


def zapis_stav(zdravi, nyni, cesta=None, ai=True):
    """Zdraví soudů a denní spotřeba AI (podle pacifického dne free tieru).
    Po objevování se zapisuje bez spotřeby (`ai=False`) – ta se přičte
    jednou, na konci běhu."""
    cesta = cesta or os.path.join(DATA_DIR, "stav.json")
    stav = nacti_stav(cesta)
    stav.setdefault("soudy", {}).update(zdravi)
    den = datetime.now(PACIFIK).date().isoformat()
    dny = stav.setdefault("ai", {})
    dnes = dny.setdefault(den, {})
    for m, sp in (fc.ai_spotreba() if ai else {}).items():
        cil = dnes.setdefault(m, {"volani": 0, "vstup": 0, "vystup": 0})
        for k in cil:
            cil[k] += sp.get(k, 0)
    hranice = (datetime.now(PACIFIK).date() - timedelta(days=STAV_DNI)).isoformat()
    stav["ai"] = {d: v for d, v in sorted(dny.items()) if d >= hranice}
    stav["aktualizovano"] = model.iso(nyni)
    zapis_atomicky(cesta, json.dumps(stav, ensure_ascii=False, indent=1, sort_keys=True) + "\n")


def _zdravi_soudu(adapter, nalezene, nove, odlozene, predtim):
    """Zdraví soudu po objevování: chyba, když zdroj nedal nic použitelného
    (adaptér ji hlásí v `chyby`), varování, když dal jen část."""
    chyby = list(getattr(adapter, "chyby", None) or [])
    varovani = list(getattr(adapter, "varovani", None) or [])
    if odlozene and not nove:
        varovani.append(f"detail nedostupný u {len(odlozene)} nových rozhodnutí, přidají se příště")
    if not nalezene and not chyby and predtim.get("nalezeno") == 0 and not predtim.get("chyba"):
        varovani.append(f"nic nenalezeno ani v minulém běhu – za {LOOKBACK_DNI} dní nic "
                        "nového? (změna webu soudu)")
    return chyby, varovani


def _ulozit_a_exportovat(sklady, nyni):
    for soud, sklad in sklady.items():
        sklad.uloz()
        if sklad.exportuj(nyni, model.OKNA_DNI[soud]):
            print(f"[{soud}] okno pro web aktualizováno")


def jen_export(soudy, nyni=None, data_dir=None, web_dir=None):
    """Okna pro web znovu z uloženého archivu – bez objevování a AI. Pro
    workflow po přerušeném běhu (vypršený krok ukončí Python i bez `finally`)."""
    nyni = nyni or model.ted()
    kw = {k: v for k, v in (("data_dir", data_dir), ("web_dir", web_dir)) if v}
    _ulozit_a_exportovat({soud: Sklad(soud, **kw).nacti(nyni) for soud in soudy}, nyni)


def beh(adaptery, soudy, nyni=None, max_polozek=60, max_minut=20, stav_cesta=None,
        data_dir=None, web_dir=None):
    """Celý běh pro vybrané soudy. Vrací souhrn (pro výpis a testy);
    `souhrn["chyby"]` a `souhrn["varovani"]` jsou selhání zdrojů po soudech.

    `max_minut` je rozpočet celého běhu od jeho začátku – objevování, AI
    i přepisu hesel."""
    nyni = nyni or model.ted()
    rozpocet = fronta.Rozpocet(max_polozek, max_minut, time.monotonic())
    tax = Taxonomie()
    sklady, zdravi, souhrn = {}, {}, {"chyby": {}, "varovani": {}}
    predtim = nacti_stav(stav_cesta).get("soudy") or {}
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
        adapter = adaptery[soud]
        try:
            nalezene = adapter.objev(od, nyni.date())
        except Exception as e:
            traceback.print_exc()
            zdravi[soud] = {"objev": model.iso(nyni), "chyba": type(e).__name__,
                            "detail": str(e)[:300]}
            souhrn["chyby"][soud] = f"{type(e).__name__}: {e}"[:300]
            continue
        odlozene = []
        nove = zapis_nalezene(sklad, nalezene, nyni, bootstrap, adapter, odlozene)
        sklad.uloz()
        chyby, varovani = _zdravi_soudu(adapter, nalezene, nove, odlozene,
                                        predtim.get(soud) or {})
        zdravi[soud] = {"objev": model.iso(nyni), "chyba": "; ".join(chyby) or None,
                        "nalezeno": len(nalezene), "nove": len(nove)}
        if odlozene:
            zdravi[soud]["odlozeno"] = len(odlozene)
        if varovani:
            zdravi[soud]["varovani"] = varovani
            souhrn["varovani"][soud] = varovani
        if chyby:
            souhrn["chyby"][soud] = "; ".join(chyby)
        souhrn[soud] = {"nalezeno": len(nalezene), "nove": len(nove)}
        print(f"[{soud}] nalezeno {len(nalezene)}, nových {len(nove)}")

    # Okna hned po objevování: převzetí desky NS či ipcurie už je v archivu
    # a staré okno by ukazovalo nahrazený záznam (kontrola by pak zahodila
    # celý běh). Nová rozhodnutí jsou tak na webu i bez shrnutí.
    _ulozit_a_exportovat(sklady, nyni)
    zapis_stav(zdravi, nyni, stav_cesta, ai=False)

    hotovo = 0
    try:
        if fc.gemini_enabled() and sklady:
            rezerva = min(HESLA_REZERVA_MIN, max_minut / 10) * 60
            hotovo = zpracuj_ai(sklady, adaptery, nyni, tax, rozpocet, rezerva)
            print(f"AI rozborů: {hotovo}")
            prepis_hesel(sklady, nyni, rozpocet=rozpocet)
    finally:
        # I po výjimce nebo Ctrl+C (Actions při vypršení kroku posílá SIGINT).
        _ulozit_a_exportovat(sklady, nyni)
        zapis_stav(zdravi, nyni, stav_cesta)
    souhrn["ai"] = hotovo
    return souhrn
