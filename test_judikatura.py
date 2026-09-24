#!/usr/bin/env python3
"""Testy sběru judikatury (balíček judikatura/ a adaptéry NS, NSS a ÚS).

Bez sítě: weby soudů i Gemini API se simulují, stránky soudů jsou skutečné
odpovědi, které stáhla sonda (tests/fixtures/). Jde o to, co pipeline s daty
udělá – že se stejné rozhodnutí nevede dvakrát, že staré se netváří jako
nové, že archiv v gitu nemění řádky zbytečně, že AI dostane celý text
a že se výpadek jednoho zdroje nepropíše do ostatních.

Spuštění: python test_judikatura.py
"""

import base64
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import feed_common as fc
from judikatura import analyza, fronta, kontrola, mapy, migrace, model, orchestr
from judikatura.sklad import Sklad, slim
from judikatura.soudy import ns
from judikatura.soudy import ipcuria as soud_ipcuria
from judikatura.soudy import nss as soud_nss
from judikatura.soudy import sdeu as soud_sdeu
from judikatura.soudy import us as soud_us
from judikatura.soudy.web import formular_pole
from judikatura.taxonomie import SOUBOR as OBLASTI_JSON
from judikatura.taxonomie import Taxonomie

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("  OK   " if cond else "  CHYBA") + f" {name}"
          + (f" – {detail}" if detail and not cond else ""))


def dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


DOCASNE = []


def tmpdir():
    d = tempfile.mkdtemp(prefix="judikatura-test-")
    DOCASNE.append(d)
    return d


TAX = Taxonomie()
DLOUHY_TEXT = "Dovolací soud přezkoumal napadené rozhodnutí a dospěl k závěru. " * 60
SHRNUTI = ("Žalobce se domáhal zaplacení smluvní pokuty. Soud řešil, zda je ujednání "
           "přiměřené. Nejvyšší soud dovodil, že ano.")

# =====================================================================
print("1) Spisové značky a první výskyt")
# =====================================================================
check("mezery kolem lomítka pryč", model.normalizuj_spz("23 Cdo 418 / 2026") == "23 Cdo 418/2026")
check("deska i databáze mají stejný klíč",
      model.spz_klic("23 Cdo 418 / 2026") == model.spz_klic("23 Cdo 418/2026")
      == ("23cdo418/2026", ""))
check("přípona dalšího rozhodnutí se oddělí",
      model.spz_klic("28 Cdo 3880/2023- II.") == ("28cdo3880/2023", "II"),
      str(model.spz_klic("28 Cdo 3880/2023- II.")))
check("zobrazení přípony sjednocené",
      model.normalizuj_spz("28 Cdo 3880/2023- II.") == "28 Cdo 3880/2023 - II.")
check("klíč bez diakritiky", model.spz_klic("29 NSČR 18 / 2024")[0] == "29nscr18/2024")
check("senát a rejstřík", model.senat_z_spz("29 NSČR 18/2024") == (29, "NSČR"))
check("značka ÚS senát NS nemá", model.senat_z_spz("Pl. ÚS 5/24") == (None, ""))
z = model.novy_zaznam("ns", "ns:X", spz="23 Cdo 418 / 2026")
check("nový záznam má normalizovanou značku a klíč",
      z["spz"] == "23 Cdo 418/2026" and z["spz_klic"] == "23cdo418/2026" and z["ai"] is None)

NYNI = dt("2026-09-24T05:00:00Z")
check("zveřejněné dnes je nové", model.prvni_vyskyt("2026-09-24", NYNI, False) == NYNI)
check("zveřejněné předevčírem je taky nové (běhy chodí řídce)",
      model.prvni_vyskyt("2026-09-22", NYNI, False) == NYNI)
check("objevené po víc než třech dnech dostane datum zveřejnění",
      model.prvni_vyskyt("2026-09-10", NYNI, False) == dt("2026-09-10T12:00:00Z"))
check("při náběhu se bere datum zveřejnění",
      model.prvni_vyskyt("2026-09-23", NYNI, True) == dt("2026-09-23T12:00:00Z"))
check("první výskyt nikdy není v budoucnosti",
      model.prvni_vyskyt("2026-09-24", NYNI, True) == NYNI)
check("bez data zveřejnění teď", model.prvni_vyskyt("", NYNI, True) == NYNI)

stary = model.novy_zaznam("ns", "ns:X", spz="23 Cdo 1/2026", url="u", first_seen="2026-09-01T00:00:00Z",
                          ai={"shrnuti": SHRNUTI})
novy = model.novy_zaznam("ns", "ns:X", spz="23 Cdo 1/2026", url="u", pdf="p.pdf",
                         first_seen="2026-09-24T00:00:00Z", meta={"kategorie": "C"})
check("sloučení doplní PDF a metadata", model.sloucit(stary, novy)
      and stary["pdf"] == "p.pdf" and stary["meta"] == {"kategorie": "C"})
check("sloučení nesahá na shrnutí a první výskyt",
      stary["ai"] == {"shrnuti": SHRNUTI} and stary["first_seen"] == "2026-09-01T00:00:00Z")
check("podruhé už se nic nemění", not model.sloucit(stary, novy))

# =====================================================================
print("\n2) Seznam oblastí")
# =====================================================================
check("25 oblastí, id jedinečná, žádná „ostatni“",
      len(TAX.ids) == 25 and len(set(TAX.ids)) == 25 and "ostatni" not in TAX.ids, str(len(TAX.ids)))
check("tři skupiny, trestní právo patří do veřejného",
      sorted({o["skupina"] for o in TAX.oblasti}) == ["Duševní vlastnictví a technologie", "Soukromé právo", "Veřejné právo"]
      and next(o for o in TAX.oblasti if o["id"] == "trestni")["skupina"] == "Veřejné právo")
check("výchozí výběr je IP/IT",
      TAX.vychozi == ["autorske", "prumyslova_prava", "nekala_soutez", "it", "gdpr"], str(TAX.vychozi))
check("neznámé id zahodí, název převede, nejvýš tři",
      TAX.normalizuj(["Autorské právo", "it", "neexistuje", "GDPR", "zavazky"]) == ["autorske", "it", "gdpr"],
      str(TAX.normalizuj(["Autorské právo", "it", "neexistuje", "GDPR", "zavazky"])))
check("dřívější „ostatni“ se zahodí",
      TAX.normalizuj(["ostatni", "zavazky"]) == ["zavazky"] and TAX.normalizuj(["ostatni"]) == [])
check("prompt obsahuje všechna id", all(f"{i} – " in TAX.do_promptu() for i in TAX.ids))
d = tmpdir()
with open(OBLASTI_JSON, encoding="utf-8") as f:
    data = json.load(f)
data["alias"] = {"duvera": "autorske"}
with open(os.path.join(d, "oblasti.json"), "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False)
check("alias (přejmenovaná oblast) vede na nové id",
      Taxonomie(os.path.join(d, "oblasti.json")).normalizuj(["duvera"]) == ["autorske"])
with open(os.path.join(os.path.dirname(OBLASTI_JSON), "ns_senaty.json"), encoding="utf-8") as f:
    senaty = json.load(f)
check("senáty NS: výchozí 23, bez duplicit",
      senaty["vychozi"] == [23] and len({s["senat"] for s in senaty["senaty"]}) == len(senaty["senaty"])
      and any(s["senat"] == 23 for s in senaty["senaty"]))

# =====================================================================
print("\n3) Archiv a okno pro web")
# =====================================================================
DATA, WEB = tmpdir(), tmpdir()


def zaznam(id_, first_seen, soud="ns", **kw):
    kw.setdefault("spz", "23 Cdo 1/2026")
    kw.setdefault("zverejneno", first_seen[:10])
    return model.novy_zaznam(soud, id_, first_seen=first_seen, **kw)


sk = Sklad("ns", data_dir=DATA, web_dir=WEB).nacti(NYNI)
check("prázdný archiv = náběh", sk.prazdny())
sk.pridej(zaznam("ns:B", "2026-09-20T10:00:00Z"))
sk.pridej(zaznam("ns:A", "2026-09-21T10:00:00Z", ai={"heslo": "Smluvní pokuta", "shrnuti": SHRNUTI,
                                                    "oblasti": ["zavazky"], "procesni": False}))
sk.pridej(zaznam("ns:C", "2026-08-15T10:00:00Z"))
sk.uloz()
mesice = sorted(os.listdir(os.path.join(DATA, "ns")))
check("záznamy jdou do měsíce prvního výskytu",
      mesice == ["2026-08.jsonl", "2026-09.jsonl", "index.tsv"], str(mesice))
with open(os.path.join(DATA, "ns", "2026-09.jsonl"), encoding="utf-8") as f:
    radky = f.read().splitlines()
check("řádky seřazené podle id, jeden záznam na řádek",
      [json.loads(r)["id"] for r in radky] == ["ns:A", "ns:B"], str(radky)[:120])
check("klíče v řádku seřazené (stabilní diff)", radky[0].startswith('{"ai":'), radky[0][:40])

sk2 = Sklad("ns", data_dir=DATA, web_dir=WEB).nacti(NYNI)
check("po načtení archiv zná všechno", not sk2.prazdny() and sk2.ma("ns:C") and len(sk2.zaznamy) == 3)
srpen = os.path.join(DATA, "ns", "2026-08.jsonl")
os.utime(srpen, (0, 0))
sk2.zaznamy["ns:B"]["ai"] = {"heslo": "Nájem", "shrnuti": SHRNUTI, "oblasti": [], "procesni": None}
sk2.zmeneno(sk2.zaznamy["ns:B"])
sk2.uloz()
check("zapíše se jen změněný měsíc", os.stat(srpen).st_mtime == 0)
with open(os.path.join(DATA, "ns", "index.tsv"), encoding="utf-8") as f:
    check("index se jen doplňuje, nezdvojuje", len(f.read().splitlines()) == 3)

check("export okna se zapíše", sk2.exportuj(NYNI, 14))
with open(os.path.join(WEB, "ns.json"), encoding="utf-8") as f:
    okno = json.load(f)
check("v okně jen posledních 14 dní",
      [p["id"] for p in okno["polozky"]] == ["ns:A", "ns:B"], str([p["id"] for p in okno["polozky"]]))
check("do okna nejdou interní pole",
      all("stav" not in p and "meta" not in p and "ai" not in p for p in okno["polozky"]))
check("stejný obsah se znovu nezapisuje (žádný commit ani deploy navíc)",
      not sk2.exportuj(NYNI + timedelta(minutes=30), 14))
sk2.zaznamy["ns:B"]["nahrazeno"] = "ns:A"
check("nahrazený záznam z okna zmizí",
      sk2.exportuj(NYNI, 14) and [z["id"] for z in sk2.v_okne(NYNI, 14)] == ["ns:A"])

s = slim(zaznam("ns:D", "2026-09-22T00:00:00Z", oblasti_meta=["dane"], procesni_meta=True))
check("bez shrnutí poznámka, oblasti a procesní z metadat",
      s["poznamka"] and s["shrnuti"] == "" and s["oblasti"] == ["dane"] and s["procesni"] is True, str(s))
# Stav shrnutí: web podle něj píše u řádku, proč shrnutí chybí, a nad
# kartou, kolik rozhodnutí ještě čeká na AI.
check("ve frontě: připravuje se",
      (s["stav_shrnuti"], s["poznamka"]) == ("pripravuje", "Shrnutí se připravuje."), str(s))
s = slim(zaznam("ns:F", "2026-09-22T00:00:00Z", stav={"pokusy": 2, "duvod": "ai-selhani"}))
check("po chybě AI se dál připravuje", s["stav_shrnuti"] == "pripravuje", str(s))
s = slim(zaznam("ns:G", "2026-09-22T00:00:00Z", stav={"pokusy": 1, "duvod": "bez-textu"}))
check("bez textu: čeká na text od soudu",
      (s["stav_shrnuti"], s["poznamka"]) == ("ceka_na_text", "Čeká na zveřejnění textu rozhodnutí."),
      str(s))
s = slim(zaznam("ns:H", "2026-09-22T00:00:00Z", stav={"pokusy": 6, "duvod": "ai-selhani"}))
check("vyčerpané pokusy AI: nepodařilo se",
      (s["stav_shrnuti"], s["poznamka"]) == ("nepodarilo", "Shrnutí se nepodařilo připravit."),
      str(s))
s = slim(zaznam("ns:I", "2026-09-22T00:00:00Z", stav={"pokusy": 9, "duvod": "bez-textu"}))
check("na text se čeká i po mnoha pokusech", s["stav_shrnuti"] == "ceka_na_text", str(s))
s = slim(zaznam("sdeu:ipc:C-1/26", "2026-09-22T00:00:00Z", soud="sdeu", spz="C-1/26",
                druh="předběžná otázka", stav={"pokusy": 2, "duvod": "bez-textu"}))
check("předběžná otázka bez textu: čeká na otázky",
      (s["stav_shrnuti"], s["poznamka"]) == ("ceka_na_text", "Položené otázky zatím nejsou zveřejněné."),
      str(s))
s = slim(zaznam("ns:E", "2026-09-22T00:00:00Z", oblasti_meta=["dane"], procesni_meta=True,
                ai={"heslo": "H", "shrnuti": SHRNUTI, "oblasti": ["spravni"], "procesni": False}))
check("oblasti a procesní z AI mají přednost",
      s["oblasti"] == ["spravni"] and s["procesni"] is False and "poznamka" not in s
      and "stav_shrnuti" not in s, str(s))

# =====================================================================
print("\n4) AI rozbor: dotaz a odpověď")
# =====================================================================
json_odpoved = ('```json\n{"heslo": "Ochranná známka", "shrnuti": "' + SHRNUTI + '", '
                '"oblasti": ["prumyslova_prava", "neexistuje", "nekala_soutez"], "procesni": false}\n```')
v = analyza.parse(json_odpoved, TAX)
check("JSON odpověď (i v bloku kódu)",
      v and v["heslo"] == "Ochranná známka" and v["oblasti"] == ["prumyslova_prava", "nekala_soutez"]
      and v["procesni"] is False, str(v))
v = analyza.parse(f"HESLO: **Smluvní pokuta**\nSHRNUTÍ: {SHRNUTI}\nOBLASTI: zavazky, Autorské právo\n"
                  "PROCESNÍ: ne", TAX)
check("značky (Gemma), tučné písmo pryč, název oblasti převeden",
      v and v["heslo"] == "Smluvní pokuta" and v["oblasti"] == ["zavazky", "autorske"]
      and v["procesni"] is False and v["shrnuti"] == SHRNUTI, str(v))
v = analyza.parse(f"HESLO: Poplatky\nSHRNUTI: {SHRNUTI}\nOBLASTI: civilni_proces\nPROCESNI: ano", TAX)
check("značky i bez diakritiky", v and v["procesni"] is True and v["oblasti"] == ["civilni_proces"], str(v))
check("krátké shrnutí se nepoužije", analyza.parse('{"heslo": "X", "shrnuti": "Krátké."}', TAX) is None)
check("prázdná odpověď se nepoužije", analyza.parse("", TAX) is None)

rozhodnuti = model.novy_zaznam("ns", "ns:T", spz="23 Cdo 5/2026", druh="usnesení",
                               meta={"heslo_ns": "Smluvní pokuta", "kategorie": "C"},
                               oblasti_meta=["zavazky"], procesni_meta=True)
obrovsky = "Odůvodnění. " * 120_000
casti = analyza.dotaz(rozhodnuti, {"text": obrovsky})
check("modelu jde celý text, bez ořezu", len(casti) == 1 and casti[0]["text"].endswith(obrovsky.strip()),
      str(len(casti[0]["text"])))
check("dotaz nese úřední údaje a pokyn",
      "Smluvní pokuta" in casti[0]["text"] and "Pokyn ke shrnutí" in casti[0]["text"])
casti = analyza.dotaz(rozhodnuti, {"pdf": b"%PDF-1.7 obsah"})
check("bez textu jde PDF", casti[0]["inline_data"]["mime_type"] == "application/pdf"
      and base64.b64decode(casti[0]["inline_data"]["data"]) == b"%PDF-1.7 obsah")
check("pokyny podle druhu",
      analyza.pokyn({"soud": "sdeu", "druh": "stanovisko GA"}) == analyza.POKYNY["stanovisko"]
      and analyza.pokyn({"soud": "sdeu", "druh": "předběžná otázka"}) == analyza.POKYNY["otazka"]
      and analyza.pokyn({"soud": "us", "druh": "nález"}) == analyza.POKYNY["us"])
check("schéma povolí jen známá id", analyza.schema(TAX)["properties"]["oblasti"]["items"]["enum"] == TAX.ids)

volani = []
puvodni_ai = fc.ai_volani


def falesna_ai(odpoved, model_jmeno="gemini-test"):
    def ai(parts, system=None, schema=None, max_tokens=8192, timeout=0):
        volani.append({"parts": parts, "system": system, "schema": schema, "timeout": timeout})
        return (odpoved(parts) if callable(odpoved) else odpoved), model_jmeno
    return ai


fc.ai_volani = falesna_ai('{"heslo": "Pokuta", "shrnuti": "' + SHRNUTI + '", "oblasti": [], "procesni": null}')
v, pouzity = analyza.analyzuj(rozhodnuti, {"text": DLOUHY_TEXT}, TAX)
check("bez oblastí od AI se vezmou z metadat, procesní taky",
      v["oblasti"] == ["zavazky"] and v["procesni"] is True and pouzity == "gemini-test", str(v))
check("systémový prompt nese seznam oblastí a JSON schéma",
      "gdpr – " in volani[-1]["system"] and volani[-1]["schema"]["type"] == "OBJECT")
PRAVNI_FORMY = [
    ("Mailboxde.cz s.r.o. se soudila s Úřadem", "Mailboxde.cz se soudila s Úřadem"),
    ("Uniphone, s.r.o. se soudila", "Uniphone se soudila"),
    ("Spolek Radslavská zátoka, z. s., a Spolek chatařů, z. s., podali žalobu",
     "Spolek Radslavská zátoka a Spolek chatařů podali žalobu"),
    ("Vodafone Czech Republic a. s. se domáhala", "Vodafone Czech Republic se domáhala"),
    ("LENDIGO Services s. r. o. vedla exekuci", "LENDIGO Services vedla exekuci"),
    ("Žalobkyní byla Alfa s.r.o. Soud rozhodl.", "Žalobkyní byla Alfa. Soud rozhodl."),
    ("Gamma GmbH & Co. KG a Delta sp. z o.o. se přely", "Gamma a Delta se přely"),
    ("BASF AG a Bayer AG proti Komisi.", "BASF a Bayer proti Komisi."),
    ("Stanovisko GA se týká toho, zda se SE musí", "Stanovisko GA se týká toho, zda se SE musí"),
]
spatne = [(v, fc.bez_pravni_formy(v)) for v, c in PRAVNI_FORMY if fc.bez_pravni_formy(v) != c]
check("právní formy ze shrnutí pryč (s.r.o., a. s., z. s. v čárkách, GmbH, AG…), „GA“ a „se“ zůstanou",
      not spatne, str(spatne))
s = slim(zaznam("ns:PF", "2026-09-22T00:00:00Z", ai={"heslo": "Alfa s.r.o.", "oblasti": ["zavazky"], "procesni": False,
                                                    "shrnuti": "Alfa, a.s., se soudila s Betou s.r.o. o zaplacení."}))
check("i hotová shrnutí na webu jsou bez právních forem",
      s["shrnuti"] == "Alfa se soudila s Betou o zaplacení." and s["heslo"] == "Alfa", str(s))
fc.ai_volani = falesna_ai('{"heslo": "Známka", "shrnuti": "Mailboxde.cz s.r.o. se soudila s Úřadem průmyslového '
                          'vlastnictví o zápis známky.", "oblasti": ["prumyslova_prava"], "procesni": false}')
v, _ = analyza.analyzuj(rozhodnuti, {"text": DLOUHY_TEXT}, TAX)
check("nové shrnutí od AI je bez právní formy", v["shrnuti"].startswith("Mailboxde.cz se soudila"), str(v))
check("prompt chce vždy aspoň jednu oblast, „ostatni“ nezná",
      "Vždy vyber aspoň jednu" in volani[-1]["system"] and "ostatni" not in volani[-1]["system"])
fc.ai_volani = falesna_ai('{"heslo": "Pokuta", "shrnuti": "' + SHRNUTI + '", "oblasti": ["ostatni"], "procesni": false}')
nahradni = {}
for klic, zz in (("ns-civilni", dict(rozhodnuti, oblasti_meta=[])),
                 ("ns-trestni", dict(rozhodnuti, oblasti_meta=[], rejstrik="Tdo")),
                 ("nss", dict(rozhodnuti, soud="nss", oblasti_meta=[])),
                 ("us", dict(rozhodnuti, soud="us", oblasti_meta=[])),
                 ("sdeu", dict(rozhodnuti, soud="sdeu", oblasti_meta=[]))):
    nahradni[klic] = analyza.analyzuj(zz, {"text": DLOUHY_TEXT}, TAX)[0]["oblasti"]
check("bez oblasti od AI i z metadat: nejčastější oblast soudu, nikdy „ostatni“",
      nahradni == {"ns-civilni": ["civilni_proces"], "ns-trestni": ["trestni"], "nss": ["spravni"],
                   "us": ["ustavni"], "sdeu": ["spravni"]}, str(nahradni))
fc.ai_volani = falesna_ai("")
check("bez odpovědi nic", analyza.analyzuj(rozhodnuti, {"text": DLOUHY_TEXT}, TAX) == (None, ""))
fc.ai_volani = falesna_ai('Tady: [{"id": "ns:T", "oblasti": ["zavazky", "xyz"], "procesni": "ne"}]')
check("dávková klasifikace migrace",
      analyza.klasifikuj_davku([dict(rozhodnuti, ai={"heslo": "H", "shrnuti": SHRNUTI})], TAX)
      == {"ns:T": (["zavazky"], False)})
fc.ai_volani = falesna_ai("nevím")
check("nečitelná klasifikace = nic", analyza.klasifikuj_davku([rozhodnuti], TAX) == {})

# =====================================================================
print("\n5) Fronta a rozpočet")
# =====================================================================
FD, FW = tmpdir(), tmpdir()
ns_sklad = Sklad("ns", data_dir=FD, web_dir=FW).nacti(NYNI)
nss_sklad = Sklad("nss", data_dir=FD, web_dir=FW).nacti(NYNI)
for z in (
    zaznam("ns:vecne", "2026-09-22T10:00:00Z", spz="30 Cdo 1/2026", senat=30),
    zaznam("ns:23", "2026-09-20T10:00:00Z", spz="23 Cdo 2/2026", senat=23),
    zaznam("ns:procesni", "2026-09-23T10:00:00Z", spz="30 Nd 3/2026", senat=30, procesni_meta=True),
    zaznam("ns:vecne-starsi", "2026-09-18T10:00:00Z", spz="30 Cdo 4/2026", senat=30),
    zaznam("ns:hotove", "2026-09-23T10:00:00Z", senat=30,
           ai={"shrnuti": SHRNUTI, "pv": analyza.PROMPT_VERZE}),
    zaznam("ns:pozdeji", "2026-09-23T10:00:00Z", senat=30,
           stav={"pokusy": 1, "dalsi_pokus": "2026-09-24T07:00:00Z", "duvod": "bez-textu"}),
    zaznam("ns:vzdane", "2026-09-23T10:00:00Z", senat=30,
           stav={"pokusy": fronta.MAX_POKUSU, "dalsi_pokus": None, "duvod": "ai-selhani"}),
    zaznam("ns:nahrazene", "2026-09-23T10:00:00Z", senat=23, nahrazeno="ns:23"),
    zaznam("ns:mimo-okno", "2026-08-01T10:00:00Z", senat=23),
):
    ns_sklad.pridej(z)
for z in (zaznam("nss:1", "2026-09-22T10:00:00Z", soud="nss", spz="", oblasti_meta=["dane"]),
          zaznam("nss:2", "2026-09-21T10:00:00Z", soud="nss", spz="", oblasti_meta=["gdpr"])):
    nss_sklad.pridej(z)
poradi = [z["id"] for z in fronta.sestav({"ns": ns_sklad}, NYNI, TAX)]
check("výchozí výběr první, pak věcná od nejnovějšího, procesní nakonec",
      poradi == ["ns:23", "ns:vecne", "ns:vecne-starsi", "ns:procesni"], str(poradi))
poradi = [z["id"] for z in fronta.sestav({"ns": ns_sklad, "nss": nss_sklad}, NYNI, TAX)]
check("soudy se střídají", poradi == ["ns:23", "nss:2", "ns:vecne", "nss:1", "ns:vecne-starsi",
                                      "ns:procesni"], str(poradi))
check("po odkladu se položka vrátí",
      "ns:pozdeji" in [z["id"] for z in fronta.sestav({"ns": ns_sklad}, NYNI + timedelta(hours=3), TAX)])
z = zaznam("ns:o", "2026-09-22T10:00:00Z")
odklady = []
for _ in range(7):
    fronta.odlozit(z, NYNI, "bez-textu")
    odklady.append(int((dt(z["stav"]["dalsi_pokus"]) - NYNI).total_seconds() // 3600))
check("odklad 1, 2, 4… hodin, nejvýš 20 (sběr je jednou denně)", odklady == [1, 2, 4, 8, 16, 20, 20],
      str(odklady))
check("na text se čeká dál, dokud je v okně", fronta.potrebuje_ai(z, NYNI + timedelta(days=2)))
for _ in range(fronta.MAX_POKUSU):
    fronta.odlozit(z, NYNI, "ai-selhani")
check("po šesti chybách AI se to vzdá", not fronta.potrebuje_ai(z, NYNI + timedelta(days=2)))
r = fronta.Rozpocet(2, 10)
r.zapocitej()
check("rozpočet položek", r.dalsi() and (r.zapocitej() or not r.dalsi()))
check("rozpočet minut", not fronta.Rozpocet(5, 0).dalsi())

# =====================================================================
print("\n6) Běh: objevení, náběh, deska, AI, výpadky")
# =====================================================================


class Adapter:
    """Simulovaný soud: objev vrací kopie připravených záznamů."""

    def __init__(self, soud, zaznamy=(), detaily=None, texty=None, chyba=None):
        self.soud = soud
        self.zaznamy = list(zaznamy)
        self.detaily = detaily or {}
        self.texty = texty or {}
        self.chyba = chyba
        self.bez_detailu = set()
        self.objev_od = []
        self.doplneno = []

    def objev(self, od, do):
        self.objev_od.append(od)
        if self.chyba:
            raise self.chyba
        return [json.loads(json.dumps(z)) for z in self.zaznamy]

    def doplnit(self, z):
        self.doplneno.append(z["id"])
        if z["id"] in self.bez_detailu:
            raise OSError("detail nejde stáhnout")
        z.update(self.detaily.get(z["id"], {}))

    def text(self, z):
        t = self.texty.get(z["id"], DLOUHY_TEXT)
        return {"text": t, "zdroj": "html"} if t else {}


def objeveny(id_, spz, zverejneno=""):
    senat, rejstrik = model.senat_z_spz(spz)
    return model.novy_zaznam("ns", id_, spz=spz, senat=senat, rejstrik=rejstrik, zverejneno=zverejneno,
                             url=f"https://rozhodnuti.nsoud.cz/{id_}")


def ai_odpoved(parts):
    return json.dumps({"heslo": "Pokuta", "shrnuti": SHRNUTI, "oblasti": ["zavazky"], "procesni": False})


fc.gemini_enabled = lambda: True
fc._poradi = ["gemini-test"]
fc.ai_volani = falesna_ai(ai_odpoved)
BD, BW = tmpdir(), tmpdir()
STAV = os.path.join(BD, "stav.json")


def beh(adaptery, nyni, **kw):
    volani.clear()
    return orchestr.beh(adaptery, list(adaptery), nyni=nyni, stav_cesta=STAV, data_dir=BD, web_dir=BW, **kw)


def archiv(soud="ns"):
    return Sklad(soud, data_dir=BD, web_dir=BW).nacti(NYNI + timedelta(days=5)).zaznamy


# Náběh: prázdný archiv, datum zveřejnění zná až detail.
a = Adapter("ns", [objeveny("ns:1", "23 Cdo 1/2026"), objeveny("ns:2", "30 Cdo 2/2026"),
                   objeveny("ns:3", "23 Cdo 3/2026")],
            detaily={"ns:1": {"zverejneno": "2026-09-18"}, "ns:2": {"zverejneno": "2026-09-22"},
                     "ns:3": {"zverejneno": "2026-09-24", "druh": "rozsudek"}})
souhrn = beh({"ns": a}, NYNI)
arch = archiv()
check("náběh hledá týden zpět", a.objev_od == [(NYNI - timedelta(days=7)).date()], str(a.objev_od))
check("detail se doplní před prvním výskytem", sorted(a.doplneno) == ["ns:1", "ns:2", "ns:3"]
      and arch["ns:3"]["druh"] == "rozsudek")
check("při náběhu staré nové nejsou",
      arch["ns:1"]["first_seen"] == "2026-09-18T12:00:00Z" and arch["ns:2"]["first_seen"] == "2026-09-22T12:00:00Z"
      and all(z["bootstrap"] for z in arch.values()), str({k: v["first_seen"] for k, v in arch.items()}))
check("dnešní rozhodnutí je nové i při náběhu", arch["ns:3"]["first_seen"] == model.iso(NYNI))
check("AI shrne všechna tři", souhrn["ai"] == 3 and all(z["ai"]["shrnuti"] == SHRNUTI for z in arch.values()),
      str(souhrn))
check("u shrnutí je model, verze promptu a zdroj textu",
      arch["ns:1"]["ai"]["model"] == "gemini-test" and arch["ns:1"]["ai"]["pv"] == analyza.PROMPT_VERZE
      and arch["ns:1"]["ai"]["zdroj"] == "html")
with open(os.path.join(BW, "ns.json"), encoding="utf-8") as f:
    okno = json.load(f)
check("okno pro web se zapíše", len(okno["polozky"]) == 3 and okno["okno_dni"] == 14)
with open(STAV, encoding="utf-8") as f:
    stav = json.load(f)
check("zdraví soudu ve stav.json", stav["soudy"]["ns"]["nove"] == 3 and stav["soudy"]["ns"]["chyba"] is None,
      str(stav.get("soudy")))

# Další den: dvě nová (dnešní a pozdě objevené staré), tři známá.
DRUHY_DEN = NYNI + timedelta(days=1)
a.zaznamy += [objeveny("ns:4", "25 Cdo 4/2026"), objeveny("ns:5", "25 Cdo 5/2026"),
              objeveny("ns:6", "25 Cdo 6/2026")]
a.detaily.update({"ns:4": {"zverejneno": "2026-09-25"}, "ns:5": {"zverejneno": "2026-09-15"},
                  "ns:6": {"zverejneno": "2026-09-16"}})
a.bez_detailu = {"ns:6"}
a.zaznamy[0]["pdf"] = "https://rozhodnuti.nsoud.cz/1.pdf"
a.doplneno.clear()
souhrn = beh({"ns": a}, DRUHY_DEN)
arch = archiv()
check("další běhy hledají deset dní zpět", a.objev_od[-1] == (DRUHY_DEN - timedelta(days=10)).date())
check("známá rozhodnutí se nezdvojí, nová přibudou", souhrn["ns"]["nove"] == 2 and len(arch) == 5, str(souhrn))
check("detail se stahuje jen u nových", sorted(a.doplneno) == ["ns:4", "ns:5", "ns:6"], str(a.doplneno))
check("bez detailu (a data zveřejnění) záznam počká na další běh", "ns:6" not in arch)
check("dnešní je nové, pozdě objevené staré ne",
      arch["ns:4"]["first_seen"] == model.iso(DRUHY_DEN) and arch["ns:5"]["first_seen"] == "2026-09-15T12:00:00Z")
check("známému se doplní PDF, shrnutí zůstane",
      arch["ns:1"]["pdf"].endswith("1.pdf") and arch["ns:1"]["ai"]["shrnuti"] == SHRNUTI)
check("AI jen pro nová", souhrn["ai"] == 2 and len(volani) == 2, str(souhrn))
a.bez_detailu = set()
beh({"ns": a}, DRUHY_DEN + timedelta(hours=2))
check("příští běh ho přidá se správným prvním výskytem",
      archiv()["ns:6"]["first_seen"] == "2026-09-16T12:00:00Z")
a.zaznamy = [z for z in a.zaznamy if z["id"] != "ns:6"]

# Úřední deska: rozsudek je vyhlášený dřív, než ho zveřejní databáze.
deska = model.novy_zaznam("ns", "ns:deska:23cdo418/2026", spz="23 Cdo 418 / 2026", senat=23,
                          rejstrik="Cdo", druh="rozsudek", zverejneno="2026-09-25",
                          pdf="https://www.nsoud.cz/deska.pdf")
a2 = Adapter("ns", [deska])
beh({"ns": a2}, DRUHY_DEN)
db = objeveny("ns:DB418", "23 Cdo 418/2026")
a2.zaznamy = [deska, db]
a2.detaily = {"ns:DB418": {"zverejneno": "2026-09-28"}}
TRETI_DEN = DRUHY_DEN + timedelta(days=3)
beh({"ns": a2}, TRETI_DEN)
arch = archiv()
check("databáze převezme od desky první výskyt i shrnutí",
      arch["ns:DB418"]["first_seen"] == arch["ns:deska:23cdo418/2026"]["first_seen"] == model.iso(DRUHY_DEN)
      and arch["ns:DB418"]["ai"]["shrnuti"] == SHRNUTI)
check("deska se označí jako nahrazená", arch["ns:deska:23cdo418/2026"]["nahrazeno"] == "ns:DB418")
with open(os.path.join(BW, "ns.json"), encoding="utf-8") as f:
    ids = [p["id"] for p in json.load(f)["polozky"]]
check("na webu je rozhodnutí jen jednou", "ns:DB418" in ids and "ns:deska:23cdo418/2026" not in ids, str(ids))
db2 = objeveny("ns:DB500", "23 Cdo 500/2026", zverejneno="2026-09-27")
deska2 = model.novy_zaznam("ns", "ns:deska:23cdo500/2026", spz="23 Cdo 500 / 2026", senat=23,
                           rejstrik="Cdo", druh="rozsudek", zverejneno="2026-09-26")
a2.zaznamy = [db2]
beh({"ns": a2}, TRETI_DEN)
a2.zaznamy = [db2, deska2]
beh({"ns": a2}, TRETI_DEN + timedelta(hours=2))
check("deska se nepřidá, když databáze rozhodnutí už má", "ns:deska:23cdo500/2026" not in archiv())

# Výpadky: soud bez odpovědi, AI bez odpovědi, rozhodnutí bez textu.
fc.ai_volani = falesna_ai("")
nss = Adapter("nss", [model.novy_zaznam("nss", "nss:1", zverejneno="2026-09-27"),
                      model.novy_zaznam("nss", "nss:2", zverejneno="2026-09-27")],
              texty={"nss:2": ""})
souhrn = beh({"ns": Adapter("ns", chyba=RuntimeError("500")), "nss": nss}, TRETI_DEN)
arch_nss = archiv("nss")
with open(STAV, encoding="utf-8") as f:
    stav = json.load(f)
check("výpadek jednoho soudu nezastaví ostatní",
      stav["soudy"]["ns"]["chyba"] == "RuntimeError" and souhrn["nss"]["nove"] == 2, str(stav["soudy"]))
check("když AI neodpoví, zkusí se to za hodinu",
      arch_nss["nss:1"]["stav"]["duvod"] == "ai-selhani" and arch_nss["nss:1"]["stav"]["pokusy"] == 1
      and arch_nss["nss:1"]["stav"]["dalsi_pokus"] == model.iso(TRETI_DEN + timedelta(hours=1)))
check("bez textu se AI nevolá",
      arch_nss["nss:2"]["stav"]["duvod"] == "bez-textu" and len(volani) == 1, str(len(volani)))


def pretizena_ai(parts, system=None, schema=None, max_tokens=8192, timeout=0):
    volani.append(parts)
    fc._posledni_pretizeni = True
    return "", ""


fc.ai_volani = pretizena_ai
volani.clear()
pretizene = Adapter("nss", [model.novy_zaznam("nss", "nss:3", zverejneno="2026-09-27")])
beh({"nss": pretizene}, TRETI_DEN)
z3 = archiv("nss")["nss:3"]
check("přetížená AI: pokus se rozhodnutí nepočítá a zkusí se příště",
      len(volani) == 1 and z3["stav"]["pokusy"] == 0 and not z3["stav"]["dalsi_pokus"] and not z3.get("ai"),
      str(z3["stav"]))
fc._posledni_pretizeni = False
fc.ai_volani = falesna_ai(ai_odpoved)
fc._klic_zamitnut = True
souhrn = beh({"nss": nss}, TRETI_DEN + timedelta(hours=2))
check("odmítnutý klíč = AI se v běhu už nezkouší", souhrn["ai"] == 0 and not volani)
fc._klic_zamitnut = False


def odmitnuty_klic(parts, system=None, schema=None, max_tokens=8192, timeout=0):
    volani.append(parts)
    fc._klic_zamitnut = True
    return "", ""


fc.ai_volani = odmitnuty_klic
nss3 = Adapter("nss", [model.novy_zaznam("nss", f"nss:k{i}", zverejneno="2026-09-27") for i in range(3)])
souhrn = beh({"nss": nss3}, TRETI_DEN + timedelta(hours=3))
arch_nss = archiv("nss")
check("klíč odmítnutý uprostřed běhu: fronta se zastaví a rozhodnutím se pokus nepočítá",
      len(volani) == 1 and all((arch_nss[f"nss:k{i}"]["stav"] or {}).get("pokusy", 0) == 0 for i in range(3)),
      str([(arch_nss[f"nss:k{i}"]["stav"]) for i in range(3)]))
fc._klic_zamitnut = False
fc.ai_volani = falesna_ai(ai_odpoved)
chyby, varovani = kontrola.zkontroluj(("ns", "nss"), data_dir=BD, web_dir=BW)
check("archiv po bězích projde kontrolou", not chyby and not varovani, str(chyby + varovani)[:300])

# =====================================================================
print("\n7) Nejvyšší soud: stránky a hledání")
# =====================================================================
DETAIL_URL = ns.HOST + "/Judikatura/judikatura_ns.nsf/WebSearch/{}?openDocument"


def radek_seznamu(unid, spz, soud="Nejvyšší soud", pdf=True, ids=True, kategorie="E"):
    return ("<tr>"
            + (f'<td><input type="checkbox" name="ids" value="{unid}"></td>' if ids else "<td></td>")
            + f'<td><a class="odk" href="/Judikatura/judikatura_ns.nsf/WebSearch/{unid}?openDocument">{spz}</a></td>'
            + f'<td class="td-short-wrap">{soud}</td><td class="category">{kategorie}</td>'
            + ('<td class="icons"><a href="/Judikatura/att.nsf/at/X/$file/'
               + spz.replace("/", "_") + '.pdf?openElement">PDF</a></td>' if pdf else '<td class="icons"></td>')
            + "</tr>")


def seznam(*radky):
    return '<html><body><table id="tabl">' + "".join(radky) + "</table></body></html>"


U1, U2, U3 = "A" * 32, "B" * 32, "C" * 32
out = ns.parse_seznam(seznam(radek_seznamu(U1, "23 Cdo 418/2026"),
                             radek_seznamu(U2, "7 Cmo 5/2025", soud="Vrchní soud v Praze"),
                             radek_seznamu(U3, "22 Nd 55/2026", pdf=False, ids=False)))
check("jen Nejvyšší soud, ne nižší soudy ze Sbírky", [z["id"] for z in out] == [f"ns:{U1}", f"ns:{U3}"],
      str([z["id"] for z in out]))
check("odkaz na detail a PDF", out[0]["url"] == DETAIL_URL.format(U1)
      and out[0]["pdf"].startswith(ns.HOST + "/Judikatura/att.nsf/") and out[1]["pdf"] == "")
check("senát, rejstřík, kategorie", out[0]["senat"] == 23 and out[0]["rejstrik"] == "Cdo"
      and out[0]["meta"]["kategorie"] == "E")
check("UNID i z odkazu, Nd je procesní podle rejstříku", out[1]["procesni_meta"] and not out[0]["procesni_meta"])


def stranka_detailu(telo, heslo="Smluvní pokuta<br>Přípustnost dovolání"):
    meta = [("Soud", "Nejvyšší soud"), ("Datum rozhodnutí", "20.08.2026"), ("Spisová značka", "23 Cdo 418/2026"),
            ("ECLI", "ECLI:CZ:NS:2026:23.CDO.418.2026.1"), ("Typ rozhodnutí", "USNESENÍ"), ("Heslo", heslo),
            ("Dotčené předpisy", "§ 2048 o. z.<br>§ 237 o. s. ř."), ("Kategorie rozhodnutí", "C"),
            ("Zveřejněno na webu", "18.09.2026")]
    radky = "".join(f'<tr><td class="left-part">{k}:</td><td class="right-part">{v}</td></tr>' for k, v in meta)
    return (f'<html><body><nav>Judikatura Vyhledávání</nav><table id="tabl">{radky}</table>'
            f"<div>{telo}</div><footer>Nejvyšší soud, Burešova 20, Brno</footer></body></html>")


d = ns.parse_detail(stranka_detailu(DLOUHY_TEXT))
check("metadata detailu", d["datum"] == "2026-08-20" and d["zverejneno"] == "2026-09-18"
      and d["druh"] == "usnesení" and d["ecli"].startswith("ECLI:CZ:NS")
      and d["meta"]["heslo_ns"] == "Smluvní pokuta; Přípustnost dovolání"
      and d["meta"]["predpisy"] == "§ 2048 o. z.; § 237 o. s. ř.", str(d["meta"]))
check("text bez tabulky metadat, navigace a patičky",
      d["text"].startswith("Dovolací soud") and "Smluvní pokuta" not in d["text"]
      and "Burešova" not in d["text"] and "Vyhledávání" not in d["text"], d["text"][:80])
check("stránka bez odůvodnění textem není",
      ns.parse_detail(stranka_detailu("Text rozhodnutí není k dispozici."))["text"] == "")

DESKA_HTML = ("<html><body><table><tr><th>Spisová značka</th><th>Vyhlášeno</th><th></th></tr>"
              '<tr><td>23 Cdo 418 / 2026</td><td>24.09.2026</td><td><a href="/fileadmin/user_upload/'
              'Uredni_deska/UD_-_civilni/Vyhlas._zneni_rozsudku_23_Cdo_418_2026.pdf">PDF</a></td></tr>'
              '<tr><td>28 Cdo 3880/2023- II.</td><td>2. 9. 2026</td><td></td></tr>'
              "<tr><td>Oznámení</td><td>1.9.2026</td><td></td></tr></table></body></html>")
out = ns.parse_deska(DESKA_HTML)
check("deska: značky, data, id", [(z["id"], z["zverejneno"]) for z in out] == [
    ("ns:deska:23cdo418/2026", "2026-09-24"), ("ns:deska:28cdo3880/2023-II", "2026-09-02")],
    str([(z["id"], z["zverejneno"]) for z in out]))
check("deska: rozsudek, odkaz na PDF na webu NS",
      out[0]["druh"] == "rozsudek" and out[0]["pdf"].startswith("https://www.nsoud.cz/fileadmin/")
      and out[0]["url"] == out[0]["pdf"])


class Odp:
    def __init__(self, status=200, text="", content=b""):
        self.status_code = status
        self.text = text
        self.content = content or text.encode("utf-8")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise OSError(f"HTTP {self.status_code}")

    def json(self):
        return json.loads(self.text)


class Web:
    """Simulovaný web NS. `hledani(dotaz, start)` vrací Odp pro vyhledávání."""

    def __init__(self, hledani=None, stranky=None):
        self.hledani = hledani or (lambda dotaz, start: Odp(text=seznam()))
        self.stranky = stranky or {}
        self.volani = []
        self.relaci = 0

    def session(self):
        self.relaci += 1
        return self

    def get(self, url, **kw):
        self.volani.append(url)
        if url == ns.HOST + "/":
            return Odp(text="<html>úvod</html>")
        if url.startswith(ns.HLEDANI):
            q = parse_qs(urlparse(url).query)
            return self.hledani(q["Query"][0], int(q["Start"][0]))
        if url in self.stranky:
            return self.stranky[url]
        raise OSError(f"neočekávaná adresa {url}")


FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests", "fixtures", "ns")


def fixture(jmeno):
    with open(os.path.join(FIXTURES, jmeno), encoding="utf-8") as f:
        return f.read()


# Skutečná odpověď NS na hledání bez výsledků (stáhla ji sonda).
PRAZDNE = fixture("vysledky_prazdne.html")

puvodni_pocet = ns.POCET
ns.POCET = 2
ns.NS.pauza = 0
hledane = []


def hledani(dotaz, start):
    hledane.append((dotaz, start))
    if "[spzn2]=cdo" in dotaz:
        radky = {1: [radek_seznamu("1" * 32, "23 Cdo 1/2026"), radek_seznamu("2" * 32, "30 Cdo 2/2026")],
                 3: [radek_seznamu("3" * 32, "25 Cdo 3/2026")]}.get(start, [])
        return Odp(text=seznam(*radky))
    if "[spzn2]=tdo" in dotaz:
        if dotaz.startswith("[spzn1]=5 AND"):
            return Odp(text=seznam(radek_seznamu("4" * 32, "5 Tdo 4/2026")))
        if dotaz.startswith("[spzn1]="):
            return Odp(text=PRAZDNE)
        return Odp(500, "<html><h1>Error</h1>Field is too large (32K)</html>")
    return Odp(text=PRAZDNE)


web = Web(hledani, {ns.DESKY[0]: Odp(text=DESKA_HTML),
                    ns.DESKY[1]: Odp(text=fixture("deska_trestni.html"))})
adapter = ns.NS(session_factory=web.session)
nalezene = adapter.objev(date(2026, 9, 14), date(2026, 9, 24))
ids = sorted(z["id"] for z in nalezene)
check("všechny stránky výsledků, rejstřík po senátech, obě desky",
      ids == sorted([f"ns:{'1' * 32}", f"ns:{'2' * 32}", f"ns:{'3' * 32}", f"ns:{'4' * 32}",
                     "ns:deska:23cdo418/2026", "ns:deska:3tz21/2026"]), str(ids))
check("prázdný výsledek není chyba – bez opakování a dělení po senátech",
      [q for q, _ in hledane].count("[spzn2]=icdo AND [datum_predani_na_web]>=14.09.2026") == 1
      and not any(q.startswith("[spzn1]=") and "icdo" in q for q, _ in hledane))
check("hledá se po rejstřících od data zveřejnění",
      ("[spzn2]=cdo AND [datum_predani_na_web]>=14.09.2026", 1) in hledane
      and ("[spzn2]=cdo AND [datum_predani_na_web]>=14.09.2026", 3) in hledane
      and all(any(f"[spzn2]={r} AND" in q for q, _ in hledane)
              for r in ns.REJSTRIKY_CIVILNI + ns.REJSTRIKY_TRESTNI))
check("rejstřík, který spadne, se zkusí ještě jednou a pak po senátech",
      [q for q, _ in hledane].count("[spzn2]=tdo AND [datum_predani_na_web]>=14.09.2026") == 2
      and sum(1 for q, _ in hledane if q.startswith("[spzn1]=") and "tdo" in q) == len(ns.SENATY_TRESTNI))
pokusy = len([u for u in web.volani if u.startswith(ns.HLEDANI)])
check("každý dotaz s čerstvou relací přes úvodní stránku",
      web.volani.count(ns.HOST + "/") == pokusy and web.relaci >= pokusy, f"{web.relaci} relací, {pokusy} dotazů")
check("stará rozhodnutí z desky se neberou",
      "ns:deska:28cdo3880/2023-II" not in ids and "ns:deska:7tdo677/2026" not in ids)
ns.POCET = puvodni_pocet

# Skutečné stránky NS, jak je 24. 9. 2026 stáhla sonda (probe.yml).
out = ns.parse_seznam(fixture("vysledky_2026-09-23.html"))
check("skutečný výpis: 14 rozhodnutí se značkou, UNID a PDF",
      len(out) == 14 and all(len(z["id"]) == 35 and z["spz"] and z["pdf"] for z in out)
      and out[0]["spz"] == "21 Cdo 1812/2026" and out[0]["id"] == "ns:0D60F7F268508D3DC1258E7B004D2BD6",
      str([z["spz"] for z in out]))
check("skutečný výpis: odkazy absolutní, mezery zakódované",
      out[0]["url"] == DETAIL_URL.format("0D60F7F268508D3DC1258E7B004D2BD6")
      and out[0]["pdf"].startswith(ns.HOST + "/Judikatura/att.nsf/") and " " not in out[0]["pdf"]
      and out[0]["pdf"].endswith(".pdf?openElement"), out[0]["pdf"])
check("skutečný výpis: kategorie, druhé rozhodnutí ve věci, Nd procesní",
      out[0]["meta"]["kategorie"] == "E"
      and any(z["spz"] == "24 Cdo 1459/2026 - II." and z["cast"] == "II" for z in out)
      and all(z["procesni_meta"] == (z["rejstrik"] == "Nd") for z in out))
check("skutečné prázdné hledání: nic, ale platná odpověď",
      ns.parse_seznam(PRAZDNE) == [] and ns.BEZ_VYSLEDKU in PRAZDNE)
d = ns.parse_detail(fixture("detail_21cdo1812-2026.html"))
check("skutečný detail: data, druh, ECLI, heslo, předpisy, kategorie",
      d["datum"] == "2026-08-31" and d["zverejneno"] == "2026-09-23" and d["druh"] == "usnesení"
      and d["ecli"] == "ECLI:CZ:NS:2026:21.CDO.1812.2026.1"
      and d["meta"]["heslo_ns"].startswith("Zpětvzetí návrhu na zahájení řízení")
      and "§ 96 o. s. ř." in d["meta"]["predpisy"] and d["meta"]["kategorie"] == "E",
      str({k: v for k, v in d.items() if k != "text"}))
check("skutečný detail: text začíná rozhodnutím, bez navigace a poučení o citaci",
      d["text"].startswith("21 Cdo 1812/2026-156\nUSNESENÍ") and "Citace rozhodnutí" not in d["text"]
      and "Zpět na list" not in d["text"] and "Vyhledávání" not in d["text"]
      and d["text"].rstrip().endswith("senátu") and len(d["text"]) > ns.MIN_TEXT, d["text"][:80])
out = ns.parse_deska(fixture("deska_civilni.html"))
check("skutečná civilní deska: 15 rozsudků s datem a PDF",
      len(out) == 15 and out[0]["id"] == "ns:deska:30cdo1765/2026" and out[0]["datum"] == "2026-09-09"
      and out[0]["zverejneno"] == "2026-09-09" and out[0]["druh"] == "rozsudek"
      and all(z["pdf"].startswith("https://www.nsoud.cz/fileadmin/") for z in out),
      str([(z["id"], z["zverejneno"]) for z in out[:3]]))
out = ns.parse_deska(fixture("deska_trestni.html"))
check("skutečná trestní deska", [(z["spz"], z["zverejneno"]) for z in out]
      == [("7 Tdo 677/2026", "2026-09-09"), ("3 Tz 21/2026", "2026-09-22")], str(out))

# Detail a text: stránka → text PDF → PDF modelu.
z = model.novy_zaznam("ns", f"ns:{U1}", spz="23 Cdo 418/2026", url=DETAIL_URL.format(U1), pdf="https://x/1.pdf")
web = Web(stranky={z["url"]: Odp(text=stranka_detailu(DLOUHY_TEXT))})
adapter = ns.NS(session_factory=web.session)
adapter.doplnit(z)
check("doplnění z detailu", z["zverejneno"] == "2026-09-18" and z["druh"] == "usnesení"
      and z["meta"]["heslo_ns"].startswith("Smluvní pokuta") and z["ecli"])
volani_pred = len(web.volani)
obsah = adapter.text(z)
check("text z detailu se podruhé nestahuje",
      obsah == {"text": ns.parse_detail(stranka_detailu(DLOUHY_TEXT))["text"], "zdroj": "html"}
      and len(web.volani) == volani_pred)

kratka = Odp(text=stranka_detailu("Nic."))
web = Web(stranky={z["url"]: kratka, z["pdf"]: Odp(content=b"%PDF-1.7 ...")})
adapter = ns.NS(session_factory=web.session)
puvodni_pdf_text = ns.pdf_text
ns.pdf_text = lambda data: DLOUHY_TEXT
check("bez textu na stránce text z PDF", adapter.text(z) == {"text": DLOUHY_TEXT, "zdroj": "pdf-text"})
ns.pdf_text = lambda data: ""
check("naskenované PDF jde modelu celé", adapter.text(z) == {"pdf": b"%PDF-1.7 ...", "zdroj": "pdf"})
ns.pdf_text = puvodni_pdf_text
web.stranky[z["pdf"]] = Odp(text="<html>Chyba</html>")
check("chybová stránka místo PDF se modelu neposílá", adapter.text(z) == {})
check("PDF bez textu z pypdf = prázdný text", ns.pdf_text(b"%PDF-1.7 broken") == "")
d_z = model.novy_zaznam("ns", "ns:deska:23cdo418/2026", spz="23 Cdo 418/2026",
                        url="https://www.nsoud.cz/d.pdf", pdf="https://www.nsoud.cz/d.pdf")
web = Web(stranky={d_z["pdf"]: Odp(content=b"%PDF-1.7 ...")})
adapter = ns.NS(session_factory=web.session)
adapter.doplnit(d_z)
check("deska nemá detail – nic se nestahuje", web.volani == [])
check("deska: rovnou PDF", adapter.text(d_z)["zdroj"] == "pdf" and web.volani == [d_z["pdf"]])

# =====================================================================
print("\n8) NSS, ÚS a SDEU: stránky, hledání, první zařazení")
# =====================================================================
KOREN_FX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests", "fixtures")


def fx(soud, jmeno, binarne=False):
    with open(os.path.join(KOREN_FX, soud, jmeno), "rb" if binarne else "r",
              **({} if binarne else {"encoding": "utf-8"})) as f:
        return f.read()


class Relace:
    """Simulovaná requests.Session: `odpovedi(metoda, url, data)` -> Odp."""

    def __init__(self, odpovedi, zaznam):
        self.odpovedi = odpovedi
        self.zaznam = zaznam

    def get(self, url, **kw):
        self.zaznam.append(("GET", url, None))
        return self.odpovedi("GET", url, None)

    def post(self, url, data=None, **kw):
        self.zaznam.append(("POST", url, data))
        return self.odpovedi("POST", url, data)


def relace(odpovedi, zaznam):
    return lambda: Relace(odpovedi, zaznam)


soud_nss.NSS.pauza = 0

# --- NSS: výsledky hledání ---
NSS_VYSLEDKY = fx("nss", "vysledky_2026-09-21.html")
radky0 = soud_nss.parse_radky(NSS_VYSLEDKY)
radky1 = soud_nss.parse_radky(fx("nss", "dalsi_radky.html"))
check("NSS: první stránka 40 řádků z 92, dočtená 20",
      len(radky0) == 40 and soud_nss.pocet_vysledku(NSS_VYSLEDKY) == 92 and len(radky1) == 20
      and soud_nss.parse_radky(fx("nss", "dalsi_prazdne.html")) == [])
check("NSS: řádek výsledků", radky0[0] == {
    "id": "785703", "datum": "2026-09-21", "cj": "2 Azs 127/2026-41", "senat": "tříčlenný senát NSS",
    "druh": "usnesení", "vyrok": "odmítnuto", "ucastnici": "Ministerstvo vnitra, xxx"}, str(radky0[0]))
check("NSS: krajské soudy a jejich pobočky se poznají",
      sum(soud_nss.je_nss(r["senat"]) for r in radky0) == 18
      and not soud_nss.je_nss("Městský soud v Praze") and not soud_nss.je_nss("pobočka Olomouc")
      and not soud_nss.je_nss("Nejvyšší soud ČR") and soud_nss.je_nss("7členný RS NSS") and soud_nss.je_nss("kárný senát"))
url_dalsi, par_dalsi = soud_nss.parametry_dalsich(NSS_VYSLEDKY)
check("NSS: parametry dočítání ze skriptu stránky",
      url_dalsi == soud_nss.HOST + "/Home/MyResTRowsCont"
      and json.loads(par_dalsi["vyhledavaciPodminky"])[0]["TechnickyNazev"] == "aktualizovano"
      and par_dalsi["zobrazeniVysledkuId"] == "1" and "order by" in par_dalsi["resultOrder"],
      str(par_dalsi)[:200])
check("NSS: číslo jednací bez mezer navíc",
      soud_nss.cislo_jednaci("21 Afs    31/2026 -   32") == "21 Afs 31/2026-32"
      and soud_nss.cislo_jednaci("9\xa0As\xa022/2026\xa0-\xa039") == "9 As 22/2026-39")
check("NSS: procesní podle výroku",
      soud_nss.procesni("odmítnuto pro nepřijatelnost") and soud_nss.procesni("odkladný účinek: přiznání")
      and soud_nss.procesni("zastaveno") and soud_nss.procesni("nepodjatý soudce")
      and not soud_nss.procesni("zamítnuto") and not soud_nss.procesni("rozšířený senát: postoupení")
      and not soud_nss.procesni(""))

# --- NSS: detail a text ---
d = soud_nss.parse_detail(fx("nss", "detail_785620.html"))
check("NSS: detail – data, druh, ECLI, čj.",
      d["datum"] == "2026-09-18" and d["zverejneno"] == "2026-09-21" and d["druh"] == "usnesení"
      and d["ecli"] == "ECLI:CZ:NSS:2026:9.As.22.2026.39" and d["cj"] == "9 As 22/2026-39",
      str({k: v for k, v in d.items() if k != "meta"}))
check("NSS: detail – soudce, oblast úpravy, výrok, orgán, předpisy",
      d["meta"]["soudce"] == "Tomáš Herc" and d["meta"]["oblast_upravy"] == "Přestupky"
      and d["meta"]["vyrok"] == "odmítnuto pro nepřijatelnost"
      and d["meta"]["spravni_organ"] == "Magistrát hlavního města Prahy"
      and d["meta"]["predpisy"].startswith("§ 36 odst. 3 zákona č. 500/2004 Sb.; ")
      and "§ 125c odst. 1 písm. k zákona č. 361/2000 Sb." in d["meta"]["predpisy"], str(d["meta"]))
check("NSS: první zařazení podle oblasti úpravy", soud_nss.oblasti_meta(d["meta"]) == ["spravni"]
      and soud_nss.oblasti_meta(soud_nss.parse_detail(fx("nss", "detail_785607.html"))["meta"]) == ["stavebni_zp"])
text = soud_nss.text_z_txt(fx("nss", "text_785707.html", binarne=True))
check("NSS: prostý text z UTF-16 bez obrázků a nul",
      text.startswith("21 Afs 31/2026") and "ROZSUDEK" in text and "\ntakto:\n" in text
      and text.endswith("předseda senátu") and "\x00" not in text and "[OBRÁZEK]" not in text
      and len(text) > 15000, text[:80])
ASPOSE = ('<html><head><title>5 As 1/2026 - html</title></head><body><div style="-aw-headerfooter-type:'
          'header-first"><p><span>5 As 1/2026</span></p></div><p><span>Nejvyšší </span><span>správní'
          '</span><span> soud rozhodl</span></p><p><span>[OBRÁZEK]</span></p>'
          + "<p><span>Odůvodnění </span><span>kasační stížnosti.</span></p>" * 40 + "</body></html>")
text = soud_nss.text_z_html(ASPOSE.encode("utf-8"))
check("NSS: čitelná podoba – úseky do vět, bez záhlaví stránek",
      text.startswith("Nejvyšší správní soud rozhodl\nOdůvodnění kasační stížnosti.")
      and "5 As 1/2026" not in text and "[OBRÁZEK]" not in text, text[:80])

# --- NSS: hledání přes formulář a dočítání ---
zaznam_nss = []


def web_nss(metoda, url, data):
    if metoda == "GET" and url == soud_nss.HOST + "/":
        return Odp(text=fx("nss", "formular.html"))
    if metoda == "POST" and url == soud_nss.HOST + "/":
        return Odp(text=NSS_VYSLEDKY)
    if metoda == "POST" and url == soud_nss.HOST + "/Home/MyResTRowsCont":
        return Odp(text=fx("nss", "dalsi_radky.html") if data["pageNum"] == "1" else "")
    if metoda == "GET" and url.startswith(soud_nss.HOST + "/DokumentDetail/Index/"):
        return Odp(text=fx("nss", "detail_785620.html"))
    raise OSError(f"neočekávaná adresa {metoda} {url}")


nalezene = soud_nss.NSS(session_factory=relace(web_nss, zaznam_nss)).objev(date(2026, 9, 21), date(2026, 9, 21))
hledani = dict(zaznam_nss[1][2])
check("NSS: formulář se pošle celý s datem zpřístupnění od–do+1 a tokenem",
      zaznam_nss[0][:2] == ("GET", soud_nss.HOST + "/") and zaznam_nss[1][:2] == ("POST", soud_nss.HOST + "/")
      and hledani["vyhledavaciSekce[1].vyhledavaciPodminka[1].vyhledavaciPodminkaHodnota[0]."
                  "HodnotaDatumACasOd"] == "21.09.2026"
      and hledani["vyhledavaciSekce[1].vyhledavaciPodminka[1].vyhledavaciPodminkaHodnota[0]."
                  "HodnotaDatumACasDo"] == "22.09.2026"
      and hledani["__RequestVerificationToken"].startswith("CfDJ8") and "btSubmit" in hledani
      and len(zaznam_nss[1][2]) > 250, str(zaznam_nss[1][2][:3]))
check("NSS: dočítá se po stránkách, dokud stránka něco vrací",
      [z[2]["pageNum"] for z in zaznam_nss if z[1].endswith("MyResTRowsCont")] == ["1", "2"])
ocekavane = {r["id"] for r in radky0 + radky1 if soud_nss.je_nss(r["senat"])}
check("NSS: jen senáty NSS, bez opakování",
      sorted(z["id"] for z in nalezene) == sorted(f"nss:{i}" for i in ocekavane)
      and len(nalezene) == len(ocekavane) > 30, f"{len(nalezene)} vs {len(ocekavane)}")
z = next(z for z in nalezene if z["id"] == "nss:785703")
check("NSS: záznam z řádku – čj., senát, odkazy, procesní",
      z["spz"] == "2 Azs 127/2026-41" and z["senat"] == 2 and z["rejstrik"] == "Azs"
      and z["druh"] == "usnesení" and z["datum"] == "2026-09-21" and z["zverejneno"] == ""
      and z["url"] == soud_nss.HOST + "/DokumentOriginal/Html/785703"
      and z["pdf"] == soud_nss.HOST + "/DokumentOriginal/Index/785703" and z["procesni_meta"], str(z))
try:
    soud_nss.NSS(session_factory=relace(lambda m, u, d: Odp(text="<html>Údržba</html>"), [])).objev(
        date(2026, 9, 21), date(2026, 9, 21))
    check("NSS: stránka bez formuláře je chyba zdroje", False)
except RuntimeError:
    check("NSS: stránka bez formuláře je chyba zdroje", True)

z = soud_nss.zaznam_z_radku(radky0[0] | {"id": "785620", "cj": "9 As 22/2026-39", "vyrok": "zamítnuto"})
soud_nss.NSS(session_factory=relace(web_nss, [])).doplnit(z)
check("NSS: doplnění z detailu (má přednost před řádkem) – zveřejnění, ECLI, soudce, oblasti, procesní",
      z["zverejneno"] == "2026-09-21" and z["datum"] == "2026-09-18" and z["ecli"].endswith("9.As.22.2026.39")
      and z["meta"]["soudce"] == "Tomáš Herc" and z["oblasti_meta"] == ["spravni"] and z["procesni_meta"]
      and z["meta"]["ucastnici"] == "Magistrát hlavního města Prahy, xxx", str(z["meta"]))


def nss_detail(vydano, zpristupneno, oblast="Duševní vlastnictví", organ="Úřad průmyslového vlastnictví"):
    pole = {"cj": "7 As    1/2024-   50", "ecli": "ECLI:CZ:NSS:2024:7.As.1.2024.50",
            "datumvydanirozhodnuti": vydano, "aktualizovano": zpristupneno + " 10:00:00",
            "druhdokumentuavyrokrozhodnuti": "Rozsudek", "vyrokrozhodnuti": "zamítnuto",
            "oblastupravy": oblast, "soudcezpravodaj": "NOVÁK Jan"}
    telo = "".join(f'<div data-field-id="{k}"><span class="det-textitle">{k} :</span>'
                   f'<span class="det-textval"> {v}</span></div>' for k, v in pole.items())
    telo += ('<table><thead><tr><td class="det-textitle" data-field-id="nazevspravnihoorganu">Název'
             '</td></tr></thead><tbody><tr><td class="det-textval" data-field-id="nazevspravnihoorganu">'
             + organ + "</td></tr></tbody></table>")
    return "<html><body>" + telo + "</body></html>"


z = model.novy_zaznam("nss", "nss:1", spz="7 As 1/2024-50")
soud_nss.NSS(session_factory=relace(lambda m, u, d: Odp(text=nss_detail("15.01.2024", "22.09.2026")),
                               [])).doplnit(z)
check("NSS: znovu zpřístupněné staré rozhodnutí není novinka",
      z["zverejneno"] == "2024-01-15" and z["meta"]["zpristupneno"] == "2026-09-22"
      and model.prvni_vyskyt(z["zverejneno"], NYNI, False) == dt("2024-01-15T12:00:00Z"))
check("NSS: spor s ÚPV je průmyslové vlastnictví, soudce jménem napřed",
      z["oblasti_meta"] == ["prumyslova_prava"] and z["meta"]["soudce"] == "Jan Novák", str(z["oblasti_meta"]))

TEXT_NSS = fx("nss", "text_785707.html", binarne=True)
PDF_URL = soud_nss.HOST + "/DokumentOriginal/Index/1"


def web_textu(text=TEXT_NSS, html=None, pdf=None):
    def odp(metoda, url, data):
        if url.endswith("/Text/1") and text is not None:
            return Odp(content=text)
        if url.endswith("/Html/1") and html is not None:
            return Odp(content=html)
        if url == PDF_URL and pdf is not None:
            return Odp(content=pdf)
        return Odp(500, "chyba")
    return relace(odp, [])


z = model.novy_zaznam("nss", "nss:1", spz="7 As 1/2024-50")
obsah = soud_nss.NSS(session_factory=web_textu()).text(z)
check("NSS: text z prostého textu", obsah["zdroj"] == "text" and obsah["text"].startswith("21 Afs 31/2026"))
obsah = soud_nss.NSS(session_factory=web_textu(text=None, html=ASPOSE.encode("utf-8"))).text(z)
check("NSS: bez prostého textu čitelná podoba", obsah["zdroj"] == "html"
      and obsah["text"].startswith("Nejvyšší správní soud rozhodl"))
puvodni_pdf_text_nss = soud_nss.pdf_text
soud_nss.pdf_text = lambda data: ""
obsah = soud_nss.NSS(session_factory=web_textu(text=None, pdf=b"%PDF-1.7 sken")).text(z)
check("NSS: nakonec PDF modelu", obsah == {"pdf": b"%PDF-1.7 sken", "zdroj": "pdf"})
soud_nss.pdf_text = puvodni_pdf_text_nss
check("NSS: bez textu nic", soud_nss.NSS(session_factory=web_textu(text=None)).text(z) == {})
CHYBOVA = ("<html><body>" + "<p>Omlouváme se, stránka není k dispozici. Zkuste to později.</p>" * 20
           + "</body></html>").encode("utf-8")
check("NSS: chybová stránka místo textu se modelu neposílá",
      soud_nss.NSS(session_factory=web_textu(text=CHYBOVA, html=CHYBOVA)).text(z) == {})
zaznam_rel = []
adapter_nss = soud_nss.NSS(session_factory=relace(web_nss, zaznam_rel))
adapter_nss.objev(date(2026, 9, 21), date(2026, 9, 21))
relace_hledani = adapter_nss._relace
adapter_nss.doplnit(soud_nss.zaznam_z_radku(radky0[0]))
check("NSS: detail jde v relaci hledání (s jejími cookies)", adapter_nss._relace is relace_hledani
      and zaznam_rel[-1][1] == soud_nss.HOST + "/DokumentDetail/Index/785703")

# --- ÚS: výsledky, text ---
US_VYSLEDKY = fx("us", "vysledky_2026-09-21.html")
radky, celkem = soud_us.parse_vysledky(US_VYSLEDKY)
check("ÚS: výpis 11 rozhodnutí", len(radky) == 11 and celkem == 11)
r = radky[0]
check("ÚS: značka, ECLI, soudce, název, data",
      r["sz"] == "2-1922-26_1" and r["spz"] == "II. ÚS 1922/26" and r["ecli"] == "ECLI:CZ:US:2026:2.US.1922.26.1"
      and r["soudce"] == "Veronika Křesťanová" and r["nazev"].startswith("Výklad § 1042 občanského")
      and (r["datum"], r["vyhlaseno"], r["zverejneno"]) == ("2026-09-09", "2026-09-15", "2026-09-21"), str(r))
check("ÚS: předpisy, forma, význam, výroky, předmět a rejstřík",
      r["predpisy"][2] == "89/2012 Sb., § 1042" and r["forma"] == "Nález" and r["vyznam"] == "3"
      and r["vyroky"][0] == "vyhověno" and len(r["vyroky"]) == 4
      and all("/" in p for p in r["predmet"]) and len(r["predmet"]) == 3
      and "náklady řízení" in r["rejstrik"], str(r))
u = next(x for x in radky if x["spz"] == "IV. ÚS 2303/26")
check("ÚS: usnesení bez vyhlášení a názvu, odmítnutí pro nepřípustnost je procesní",
      u["forma"] == "Usnesení" and u["vyhlaseno"] == "" and u["nazev"] == "" and u["navrhovatel"] == ["STĚŽOVATEL - FO"]
      and soud_us.procesni(u["vyroky"]) and not soud_us.procesni(r["vyroky"])
      and not soud_us.procesni(["odmítnuto pro zjevnou neopodstatněnost"]), str(u))
check("ÚS: další stránka, prázdné hledání, formulář místo výsledků",
      soud_us.parse_vysledky(fx("us", "vysledky_strana2.html"))[1] == 378
      and len(soud_us.parse_vysledky(fx("us", "vysledky_strana2.html"))[0]) == 10
      and soud_us.parse_vysledky(fx("us", "vysledky_prazdne.html")) == ([], 0)
      and soud_us.parse_vysledky(fx("us", "formular.html")) == ([], None))
z = soud_us.zaznam_z_radku(next(x for x in radky if x["spz"] == "Pl. ÚS 10/26"))
check("ÚS: záznam – id, odkaz na text, druh, název, soudce",
      z["id"] == "us:Pl-10-26_2" and z["url"] == soud_us.HOST + "/Search/GetText.aspx?sz=Pl-10-26_2"
      and z["druh"] == "nález" and z["nazev"].startswith("Obnovené řízení po rozsudku ESLP")
      and z["meta"]["soudce"] == "Tomáš Langášek" and z["zverejneno"] == "2026-09-21", str(z))
check("ÚS: plénum je ústavní, trestní řád trestní, ústavní předpisy nic neřeknou",
      z["oblasti_meta"] == ["ustavni", "trestni"]
      and soud_us.zaznam_z_radku(r)["oblasti_meta"] == [], str(z["oblasti_meta"]))
check("ÚS: značky ve tvaru citace",
      soud_us.spisova_znacka("IV.ÚS 465/26 #1") == "IV. ÚS 465/26"
      and soud_us.spisova_znacka("Pl.ÚS-st. 60/24 #1") == "Pl. ÚS-st. 60/24"
      and soud_us.spisova_znacka("x", "usnesení sp. zn. III. ÚS 767/26 ze dne 12. 8. 2026") == "III. ÚS 767/26")
text = soud_us.parse_text(fx("us", "text_1-1029-26_1.html"))
check("ÚS: text rozhodnutí s formou, bez tlačítek a hlavičky stránky",
      text.startswith("NÁLEZ\n\nÚstavní soud rozhodl v senátu") and "\nOdůvodnění:\n" in text
      and text.endswith("Jan Wintr") and "Stáhnout ve formátu" not in text, text[:80])

# --- ÚS: hledání a stránkování ---
zaznam_us = []


def web_us(prvni, dalsi):
    def odp(metoda, url, data):
        if metoda == "GET" and url == soud_us.HLEDANI:
            return Odp(text=fx("us", "formular.html"))
        if metoda == "POST" and url == soud_us.HLEDANI:
            return Odp(text=prvni)
        if metoda == "GET" and url.startswith(soud_us.VYSLEDKY + "?page="):
            return Odp(text=dalsi.get(url.rsplit("=", 1)[1], fx("us", "formular.html")))
        if metoda == "GET" and url.startswith(soud_us.HOST + "/Search/GetText.aspx"):
            return Odp(text=fx("us", "text_1-1029-26_1.html"))
        raise OSError(f"neočekávaná adresa {metoda} {url}")
    return relace(odp, zaznam_us)


nalezene = soud_us.US(session_factory=web_us(US_VYSLEDKY, {})).objev(date(2026, 9, 14), date(2026, 9, 24))
post = dict(zaznam_us[1][2])
check("ÚS: formulář s viewstate, datem zpřístupnění a řazením",
      zaznam_us[1][:2] == ("POST", soud_us.HLEDANI) and post["ctl00$MainContent$availableFrom"] == "14.9.2026"
      and post["ctl00$MainContent$availableTo"] == "24.9.2026" and post["ctl00$MainContent$razeni"] == "20"
      and post["ctl00$MainContent$resultsPageSize"] == "80" and post["__VIEWSTATE"]
      and post["ctl00$MainContent$but_search"] == "Vyhledat"
      and post["ctl00$MainContent$nalezy"] == post["ctl00$MainContent$usneseni"] == "on", str(post)[:200])
check("ÚS: celý výsledek na jedné stránce – dál se nestránkuje",
      len(nalezene) == 11 and not any("page=" in z[1] for z in zaznam_us))
zaznam_us.clear()
nalezene = soud_us.US(session_factory=web_us(fx("us", "vysledky_strana2.html"), {"1": US_VYSLEDKY})).objev(
    date(2026, 8, 25), date(2026, 9, 24))
ocekavane_us = {r["sz"] for r in soud_us.parse_vysledky(fx("us", "vysledky_strana2.html"))[0] + radky}
check("ÚS: další stránky z relace, dokud něco vracejí, bez opakování",
      [z[1].rsplit("=", 1)[1] for z in zaznam_us if "page=" in z[1]] == ["1", "2"]
      and sorted(z["id"] for z in nalezene) == sorted(f"us:{sz}" for sz in ocekavane_us),
      str([z[1] for z in zaznam_us]))
try:
    soud_us.US(session_factory=web_us(fx("us", "formular.html"), {})).objev(date(2026, 9, 14), date(2026, 9, 24))
    check("ÚS: formulář místo výsledků je chyba zdroje", False)
except RuntimeError:
    check("ÚS: formulář místo výsledků je chyba zdroje", True)
obsah = soud_us.US(session_factory=web_us(US_VYSLEDKY, {})).text(nalezene[0])
check("ÚS: text z trvalé adresy", obsah["zdroj"] == "html" and obsah["text"].startswith("NÁLEZ"))


# --- SDEU: Cellar (SPARQL, XHTML) a InfoCuria ---
soud_sdeu.SDEU.pauza = 0
SPARQL_ROZH = json.loads(fx("sdeu", "sparql_rozhodnuti.json"))
SPARQL_OZN = json.loads(fx("sdeu", "sparql_oznameni.json"))
check("SDEU: číslo věci z CELEX",
      soud_sdeu.cislo_veci("62025CJ0151") == "C-151/25" and soud_sdeu.cislo_veci("62024TJ0463") == "T-463/24"
      and soud_sdeu.cislo_veci("62026CN0630") == "C-630/26" and soud_sdeu.cislo_veci("32016R0679") == "")
rozh = soud_sdeu.zaznamy_rozhodnuti(SPARQL_ROZH)
druhy = {}
for z in rozh:
    druhy[(z["druh"], z["spz"][0])] = druhy.get((z["druh"], z["spz"][0]), 0) + 1
check("SDEU: rozsudky, usnesení a stanoviska za 14 dní (bez abstraktů a výtahů)",
      len(rozh) == 81 and druhy == {("rozsudek", "C"): 33, ("stanovisko GA", "C"): 21, ("rozsudek", "T"): 17,
                                    ("usnesení", "T"): 9, ("usnesení", "C"): 1}, str(druhy))
z = next(z for z in rozh if z["id"] == "sdeu:62025CJ0151")
check("SDEU: záznam rozsudku – věc, druh, data, ECLI, odkaz",
      z["spz"] == "C-151/25" and z["druh"] == "rozsudek" and z["datum"] == z["zverejneno"] == "2026-09-24"
      and z["ecli"] == "ECLI:EU:C:2026:789" and z["url"] == "https://eur-lex.europa.eu/legal-content/CS/TXT/?uri=CELEX:62025CJ0151"
      and z["meta"] == {"soud_eu": "Soudní dvůr", "celex": "62025CJ0151"}, str(z))
ozn = soud_sdeu.zaznamy_oznameni(SPARQL_OZN)
z = next(z for z in ozn if z["spz"] == "C-630/26")
check("SDEU: nové předběžné otázky z oznámení v ÚV, kasační opravné prostředky ne",
      len(ozn) == 9 and all(z["druh"] == "předběžná otázka" for z in ozn)
      and not any(x["spz"] in ("C-768/26", "C-656/26") for x in ozn), str([x["spz"] for x in ozn]))
check("SDEU: oznámení – účastník, předkládající soud, podání a zveřejnění",
      z["nazev"] == "Rada Miasta Krakowa" and z["meta"]["predkladajici_soud"] == "Naczelny Sąd Administracyjny (Polsko)"
      and z["datum"] == "2026-06-10" and z["zverejneno"] == "2026-09-21" and z["id"] == "sdeu:62026CN0630", str(z))
dotaz = soud_sdeu.dotaz_rozhodnuti(date(2026, 9, 10), date(2026, 9, 24))
check("SDEU: dotazy SPARQL – typy zdroje a data",
      all(f"resource-type/{t}>" in dotaz for t in ("JUDG", "ORDER", "OPIN_AG"))
      and '"2026-09-10"^^xsd:date' in dotaz and '"2026-09-24"^^xsd:date' in dotaz
      and '"2026-09-10T00:00:00"^^xsd:dateTime' in soud_sdeu.dotaz_oznameni(date(2026, 9, 10)))
nazev, texty = soud_sdeu.vec_z_infocurie(json.loads(fx("sdeu", "infocuria_C-151-25.json")), "C-151/25")
check("SDEU: InfoCuria – název věci a české texty dokumentů podle CELEX, bez „null“",
      nazev == "Viaudret" and texty["62025CJ0151"].startswith("ROZSUDEK SOUDNÍHO DVORA")
      and texty["62025CC0151"].startswith("STANOVISKO GENERÁLNÍ ADVOKÁTKY"), str({k: v[:40] for k, v in texty.items()}))
check("SDEU: Tribunál zatím bez českého textu, číslo věci s příponou (PPU) se pozná",
      soud_sdeu.vec_z_infocurie(json.loads(fx("sdeu", "infocuria_T-83-22.json")), "T-83/22")
      == ("Selimfiber v. EUIPO - Qureshi (SPETRA)", {})
      and soud_sdeu.vec_z_infocurie({"searchHits": [{"content": {"publishedId": "C-1028/26 (PPU)",
                                                                 "usualNameML": [{"cs": "Gradijk"}]}}]},
                                    "C-1028/26") == ("Gradijk", {}))
text = soud_sdeu.text_z_cellaru(fx("sdeu", "cellar_62025CJ0151_fra.html"))
check("SDEU: text z Cellaru (čerstvý rozsudek francouzsky)",
      text.startswith("ARRÊT DE LA COUR (cinquième chambre)") and "Dans l’affaire C‑151/25" in text, text[:60])
text = soud_sdeu.text_z_cellaru(fx("sdeu", "cellar_62026CN0630_ces.html"))
check("SDEU: oznámení o předběžné otázce i s otázkami", "Předběžné otázky" in text
      and "směrnice o službách" in text and len(text) > soud_sdeu.MIN_TEXT, text[:60])

zaznam_sdeu = []


def web_sdeu(cellar=None, oznameni=True, infocuria=None, ipcuria_web=None):
    """cellar: {jazyk: html}; infocuria: {číslo věci: odpověď};
    ipcuria_web: {adresa: html} (bez něj ipcuria.eu neodpovídá)."""
    def odp(metoda, url, data):
        if ipcuria_web is not None and url.startswith(soud_ipcuria.HOST):
            return Odp(text=ipcuria_web[url]) if url in ipcuria_web else Odp(404, "nic")
        if url == soud_sdeu.SPARQL:
            if "resource_legal_type" in data["query"]:
                return Odp(text=json.dumps(SPARQL_OZN)) if oznameni else Odp(500, "chyba")
            return Odp(text=json.dumps(SPARQL_ROZH))
        if url == soud_sdeu.INFOCURIA:
            return Odp(text=json.dumps((infocuria or {}).get(data["publishedId"], {"searchHits": []})))
        if url.startswith("http://publications.europa.eu/resource/celex/"):
            html = (cellar or {}).get(zaznam_sdeu[-1][3])
            return Odp(text=html) if html else Odp(404, "nic")
        raise OSError(f"neočekávaná adresa {metoda} {url}")
    return relace(odp, zaznam_sdeu)


class RelaceSdeu(Relace):
    """Jako Relace, jen si u GET poznamená jazyk (Accept-Language) a POST
    posílá JSON."""

    def get(self, url, headers=None, **kw):
        self.zaznam.append(("GET", url, None, (headers or {}).get("Accept-Language")))
        return self.odpovedi("GET", url, None)

    def post(self, url, data=None, json=None, **kw):
        self.zaznam.append(("POST", url, data if data is not None else json, None))
        return self.odpovedi("POST", url, data if data is not None else json)


def relace_sdeu(**kw):
    odpovedi = web_sdeu(**kw)().odpovedi
    return lambda: RelaceSdeu(odpovedi, zaznam_sdeu)


nalezene = soud_sdeu.SDEU(session_factory=relace_sdeu()).objev(date(2026, 9, 10), date(2026, 9, 24))
check("SDEU: objevení – rozhodnutí i předběžné otázky", len(nalezene) == 90
      and sum(z["druh"] == "předběžná otázka" for z in nalezene) == 9)
nalezene = soud_sdeu.SDEU(session_factory=relace_sdeu(oznameni=False)).objev(date(2026, 9, 10), date(2026, 9, 24))
check("SDEU: když oznámení nejdou, rozhodnutí se vezmou i tak", len(nalezene) == 81)

INFOCURIA = {"C-151/25": json.loads(fx("sdeu", "infocuria_C-151-25.json")),
             "T-83/22": json.loads(fx("sdeu", "infocuria_T-83-22.json"))}
adapter_sdeu = soud_sdeu.SDEU(session_factory=relace_sdeu(infocuria=INFOCURIA))
z = next(z for z in soud_sdeu.zaznamy_rozhodnuti(SPARQL_ROZH) if z["id"] == "sdeu:62025CJ0151")
adapter_sdeu.doplnit(z)
zaznam_sdeu.clear()
obsah = adapter_sdeu.text(z)
check("SDEU: název z InfoCurie, český text se podruhé nestahuje",
      z["nazev"] == "Viaudret" and obsah["zdroj"] == "infocuria"
      and obsah["text"].startswith("ROZSUDEK SOUDNÍHO DVORA") and zaznam_sdeu == [])
z = next(z for z in soud_sdeu.zaznamy_rozhodnuti(SPARQL_ROZH) if z["spz"] == "T-83/22")
zaznam_sdeu.clear()
obsah = soud_sdeu.SDEU(session_factory=relace_sdeu(
    infocuria=INFOCURIA, cellar={"fra": fx("sdeu", "cellar_62025CJ0151_fra.html")})).text(z)
check("SDEU: bez textu v InfoCurii Cellar česky, anglicky, francouzsky",
      obsah["zdroj"] == "cellar-fra" and [x[3] for x in zaznam_sdeu if x[0] == "GET"] == ["ces", "eng", "fra"],
      str(zaznam_sdeu))
check("SDEU: nikde nic = bez textu",
      soud_sdeu.SDEU(session_factory=relace_sdeu(infocuria=INFOCURIA)).text(z) == {})
check("SDEU: pokyny AI podle druhu",
      analyza.pokyn({"soud": "sdeu", "druh": "stanovisko GA"}) == analyza.POKYNY["stanovisko"]
      and analyza.pokyn({"soud": "sdeu", "druh": "předběžná otázka"}) == analyza.POKYNY["otazka"]
      and analyza.pokyn({"soud": "sdeu", "druh": "rozsudek"}) == analyza.POKYNY["sdeu"])

# --- ipcuria.eu: rané předběžné otázky z IP ---
IPC_SEZNAM = fx("sdeu", "ipcuria_referrals.html")
IPC_VEC = fx("sdeu", "ipcuria_vec_C-1009-26.html")
seznam = soud_ipcuria.parse_seznam(IPC_SEZNAM)
podle_veci = {r["vec"]: r for r in seznam}
check("ipcuria: seznam otázek, věc uvedená dvakrát jen jednou",
      len(seznam) == 61 and seznam[0] == {"vec": "C-1009/26", "nazev": "TikTok Information Technologies UK",
                                          "podano": "2026-09-11", "kategorie": []}
      and len(podle_veci["C-222/25"]["kategorie"]) == 3, str(seznam[:1]))
check("ipcuria: oblasti podle kategorií, bez kategorie hlavní oblasti IP a údajů",
      soud_ipcuria.oblasti(podle_veci["C-691/26"]["kategorie"]) == ["prumyslova_prava"]
      and soud_ipcuria.oblasti(podle_veci["C-660/26"]["kategorie"]) == ["gdpr"]
      and soud_ipcuria.oblasti(podle_veci["C-517/26"]["kategorie"]) == ["autorske"]
      and soud_ipcuria.oblasti(podle_veci["C-196/26"]["kategorie"]) == ["prumyslova_prava", "autorske", "mps"]
      and soud_ipcuria.oblasti([]) == ["prumyslova_prava", "autorske", "gdpr"],
      str({v: soud_ipcuria.oblasti(podle_veci[v]["kategorie"]) for v in ("C-691/26", "C-196/26")}))
check("ipcuria: CELEX budoucího oznámení v ÚV",
      soud_ipcuria.celex_oznameni("C-1009/26") == "62026CN1009"
      and soud_ipcuria.celex_oznameni("C-5/25") == "62025CN0005" and soud_ipcuria.celex_oznameni("T-1/26") == "")
check("ipcuria: stránka věci bez otázek nedá text", soud_ipcuria.text_vec(IPC_VEC) == "")
IPC_WEB = {soud_ipcuria.SEZNAM: IPC_SEZNAM}
nalezene = soud_sdeu.SDEU(session_factory=relace_sdeu(ipcuria_web=IPC_WEB)).objev(date(2026, 9, 10), date(2026, 9, 24))
rane = [z for z in nalezene if z["id"].startswith("sdeu:ipc:")]
check("SDEU: z ipcuria jen otázky podané za poslední měsíc",
      len(nalezene) == 91 and [z["id"] for z in rane] == ["sdeu:ipc:C-1009/26"], str([z["id"] for z in rane]))
z = rane[0]
check("SDEU: rané otázka – druh, datum podání, bez data zveřejnění, odkaz na web Soudního dvora",
      z["druh"] == "předběžná otázka" and z["datum"] == "2026-09-11" and z["zverejneno"] == ""
      and z["url"] == "https://curia.europa.eu/juris/liste.jsf?num=C-1009/26&language=cs"
      and z["meta"]["celex"] == "62026CN1009" and z["meta"]["ipcuria"] == soud_ipcuria.VEC.format(vec="C-1009/26")
      and z["oblasti_meta"] == ["prumyslova_prava", "autorske", "gdpr"] and z["nazev"] == "TikTok Information Technologies UK",
      str(z))
check("SDEU: když ipcuria.eu nejde, zbytek se vezme i tak",
      len(soud_sdeu.SDEU(session_factory=relace_sdeu(ipcuria_web={})).objev(date(2026, 9, 10), date(2026, 9, 24))) == 90)
# Text k rané otázce: žádost v InfoCurii (DDP), pak oznámení v Cellaru, pak ipcuria.
z = soud_sdeu.zaznamy_ipcurie([{"vec": "C-151/25", "nazev": "Viaudret", "podano": "2025-02-20", "kategorie": []}],
                              date(2025, 1, 1))[0]
obsah = soud_sdeu.SDEU(session_factory=relace_sdeu(infocuria=INFOCURIA)).text(z)
check("SDEU: rané otázka – text žádosti z InfoCurie (ne rozsudek ve věci)",
      obsah.get("zdroj") == "infocuria" and obsah["text"].startswith("Shrnutí C-151/25"), str(obsah)[:120])
z = rane[0]
zaznam_sdeu.clear()
obsah = soud_sdeu.SDEU(session_factory=relace_sdeu(ipcuria_web={soud_ipcuria.VEC.format(vec="C-1009/26"): IPC_VEC})).text(z)
check("SDEU: bez žádosti a oznámení a s „otázky zatím nejsou“ = bez textu",
      obsah == {} and [x[1] for x in zaznam_sdeu if x[0] == "GET"]
      == [soud_sdeu.CELLAR.format(celex="62026CN1009")] * 3 + [soud_ipcuria.VEC.format(vec="C-1009/26")],
      str(zaznam_sdeu))
OTAZKY = IPC_VEC.replace("Questions are not yet available on the CJEU website.",
                         "Questions referred: " + "Must Article 17 of Directive 2019/790 be interpreted so that … " * 12)
obsah = soud_sdeu.SDEU(session_factory=relace_sdeu(ipcuria_web={soud_ipcuria.VEC.format(vec="C-1009/26"): OTAZKY})).text(z)
check("SDEU: otázky ze stránky ipcuria, když jinde nic není",
      obsah.get("zdroj") == "ipcuria" and "Questions referred" in obsah["text"], str(obsah)[:120])

# Oznámení v ÚV převezme ranou otázku z ipcuria.
PD = tmpdir()


def sklad_sdeu(*zaznamy):
    sk = Sklad("sdeu", data_dir=PD, web_dir=tmpdir()).nacti(NYNI)
    for x in zaznamy:
        sk.pridej(x)
    return sk


def rana(**kw):
    return zaznam("sdeu:ipc:C-1009/26", "2026-09-20T10:00:00Z", soud="sdeu", spz="C-1009/26",
                  druh="předběžná otázka", zverejneno="", **kw)


def oznameni(**kw):
    return zaznam("sdeu:62026CN1009", "2026-09-24T02:00:00Z", soud="sdeu", spz="C-1009/26",
                  druh="předběžná otázka", **kw)


HOTOVE_AI = {"heslo": "Platformy", "shrnuti": SHRNUTI, "oblasti": ["autorske"], "procesni": False}
sk = sklad_sdeu(rana(ai=HOTOVE_AI))
n = oznameni()
check("oznámení převezme ranou otázku se shrnutím (i s prvním výskytem – už byla vidět)",
      orchestr._prevezmi_predbezne(sk, n) and n["ai"] == HOTOVE_AI and n["first_seen"] == "2026-09-20T10:00:00Z"
      and sk.zaznamy["sdeu:ipc:C-1009/26"]["nahrazeno"] == "sdeu:62026CN1009", str(n["first_seen"]))
sk = sklad_sdeu(rana())
n = oznameni()
check("bez shrnutí se oznámení ukáže jako nové (s otázkami)",
      orchestr._prevezmi_predbezne(sk, n) and n["first_seen"] == "2026-09-24T02:00:00Z" and not n.get("ai")
      and sk.zaznamy["sdeu:ipc:C-1009/26"]["nahrazeno"] == "sdeu:62026CN1009")
sk = sklad_sdeu(rana(ai=HOTOVE_AI))
n = zaznam("sdeu:62026CJ1009", "2026-09-24T02:00:00Z", soud="sdeu", spz="C-1009/26", druh="rozsudek")
check("rozsudek ve stejné věci ranou otázku nepřevezme",
      orchestr._prevezmi_predbezne(sk, n) and not n.get("ai") and not sk.zaznamy["sdeu:ipc:C-1009/26"]["nahrazeno"])
sk = sklad_sdeu(oznameni())
check("raná otázka se nepřidá, když oznámení už je", not orchestr._prevezmi_predbezne(sk, rana()))

# --- mapy ---
spatne = [(k, o) for k, v in mapy.nacti("predpisy")["predpisy"].items() for o in v["oblasti"]
          if o not in TAX.ids]
spatne += [(k, o) for k, v in mapy.nacti("ipcuria")["kategorie"].items() for o in v if o not in TAX.ids]
spatne += [("bez_kategorie", o) for o in mapy.nacti("ipcuria")["bez_kategorie"] if o not in TAX.ids]
spatne += [(k, o) for sekce in ("oblast_upravy", "organy") for k, v in mapy.nacti("nss")[sekce].items()
           for o in v if o not in TAX.ids]
spatne += [(k, o) for k, v in mapy.nacti("us")["rejstrik"].items() for o in v if o not in TAX.ids]
check("mapy znají jen oblasti ze seznamu", not spatne, str(spatne))
check("předpisy z citací v různém tvaru",
      mapy.cisla_predpisu(["121/2000 Sb., § 40", "§ 4 zákona č. 441/2003 Sb.", "2/1993 Sb./Sb.m.s., čl. 36"])
      == ["121/2000", "441/2003", "2/1993"]
      and mapy.z_predpisu(["121/2000 Sb.", "§ 10 zákona č. 110/2019 Sb."]) == ["autorske", "gdpr"])
check("oblast úpravy podle skupiny před pomlčkou",
      mapy.z_oblasti_upravy("Daně - daň z příjmů") == ["dane"]
      and mapy.z_oblasti_upravy("Sociální ochrana – Zdravotní pojištění") == ["socialni_cizinci"]
      and mapy.z_oblasti_upravy("Nezpůsobilost soudce § 91 z.č. 6/2002 Sb.") == ["spravni"]
      and mapy.z_oblasti_upravy("Něco nového") == [])
check("orgány a rejstřík podle části názvu",
      mapy.z_organu(["Úřad průmyslového vlastnictví, xxx"]) == ["prumyslova_prava"]
      and mapy.z_organu(["Úřad pro ochranu osobních údajů"]) == ["gdpr"]
      and mapy.z_rejstriku(["ochranná známka", "trestní řízení"]) == ["prumyslova_prava", "trestni"])
_, pole = formular_pole(fx("nss", "formular.html"), "form#findform")
strom = next(v for n, v in pole if n.endswith("ciselnikTreeData") and "Duševní vlastnictví" in v)
oblasti_nss = [t for t in re.findall(r'title:"([^"]+)"', strom)
               if t not in ("Ostatní", "Ostatní (nekasační agenda)", "Procesní")]
bez_mapy = [t for t in oblasti_nss if not mapy.z_oblasti_upravy(t)]
check("každá oblast úpravy z vyhledávače NSS má oblast", len(oblasti_nss) > 70 and not bez_mapy, str(bez_mapy))

# --- celý běh s oběma adaptéry ---
ED, EW = tmpdir(), tmpdir()
radky_podle_id = {r["id"]: r for r in radky0 + radky1}
IP_ID = next(r["id"] for r in radky0 if soud_nss.je_nss(r["senat"]) and r["id"] != "785703")


def web_nss_behu(metoda, url, data):
    if "/DokumentDetail/Index/" in url:
        id_ = url.rsplit("/", 1)[1]
        r = radky_podle_id[id_]
        vydano = "15.01.2024" if id_ == "785703" else ".".join(reversed(r["datum"].split("-")))
        return Odp(text=nss_detail(vydano, "21.09.2026",
                                   oblast="Duševní vlastnictví" if id_ == IP_ID else "Daně - ostatní",
                                   organ="Úřad průmyslového vlastnictví" if id_ == IP_ID else "Finanční úřad"))
    if "/DokumentOriginal/Text/" in url:
        return Odp(content=TEXT_NSS)
    return web_nss(metoda, url, data)


fc.ai_volani = falesna_ai(lambda parts: json.dumps(
    {"heslo": "Heslo", "shrnuti": SHRNUTI, "oblasti": ["dane"], "procesni": False}))
zaznam_us.clear()
volani.clear()
souhrn = orchestr.beh({"nss": soud_nss.NSS(session_factory=relace(web_nss_behu, [])),
                       "us": soud_us.US(session_factory=web_us(US_VYSLEDKY, {}))},
                      ["nss", "us"], nyni=NYNI, max_polozek=200, stav_cesta=os.path.join(ED, "stav.json"),
                      data_dir=ED, web_dir=EW)
arch_nss = Sklad("nss", data_dir=ED, web_dir=EW).nacti(NYNI).zaznamy
arch_us = Sklad("us", data_dir=ED, web_dir=EW).nacti(NYNI).zaznamy
with open(os.path.join(EW, "nss.json"), encoding="utf-8") as f:
    okno_nss = json.load(f)
with open(os.path.join(EW, "us.json"), encoding="utf-8") as f:
    okno_us = json.load(f)
check("běh: náběh NSS i ÚS, všechno nalezené v archivu",
      souhrn["nss"]["nove"] == len(ocekavane) and souhrn["us"]["nove"] == 11
      and len(arch_us) == 11, str(souhrn))
check("běh: znovu zpřístupněné staré rozhodnutí je v archivu, ale ne na webu",
      "nss:785703" in Sklad("nss", data_dir=ED, web_dir=EW).nacti(NYNI).index
      and "nss:785703" not in {p["id"] for p in okno_nss["polozky"]}
      and len(okno_nss["polozky"]) == len(ocekavane) - 1, str(len(okno_nss["polozky"])))
ip = next(p for p in okno_nss["polozky"] if p["id"] == f"nss:{IP_ID}")
check("běh: okna pro web se shrnutím, první výskyt podle zpřístupnění",
      ip["shrnuti"] == SHRNUTI and ip["first_seen"] == "2026-09-21T12:00:00Z"
      and okno_us["okno_dni"] == 14 and all(p["shrnuti"] for p in okno_us["polozky"])
      and any(p.get("nazev", "").startswith("Obnovené řízení") for p in okno_us["polozky"]), str(ip))
prvni_dotaz = volani[0]["parts"][0]["text"]
check("běh: fronta vzala spor s ÚPV jako první (výchozí výběr podle úředních údajů)",
      "Úřad průmyslového vlastnictví" in prvni_dotaz and "oblast_upravy: Duševní vlastnictví" in prvni_dotaz,
      prvni_dotaz[:300])
chyby, varovani = kontrola.zkontroluj(("nss", "us"), data_dir=ED, web_dir=EW)
check("běh: archiv NSS a ÚS projde kontrolou", not chyby and not varovani, str(chyby + varovani)[:300])
fc.ai_volani = falesna_ai(ai_odpoved)

# =====================================================================
print("\n9) Migrace starého feedu 23 Cdo")
# =====================================================================
RSS = ("<rss><channel><item><title>23 Cdo 1/2026</title><link>L</link><guid>G</guid>"
       "<ai-summary>S</ai-summary><ai-tag>T</ai-tag></item><item><title>X</title></item></channel></rss>")
check("položky z RSS", [p["title"] for p in migrace.polozky(RSS)] == ["23 Cdo 1/2026", "X"]
      and next(migrace.polozky(RSS))["ai-summary"] == "S")
check("rozbitý feed se přeskočí", list(migrace.polozky("<rss>")) == [])
UNID = "13E8C436D30A0A27C1258E06004D5974"
DESKA_PDF = "https://www.nsoud.cz/fileadmin/Vyhlas._zneni_rozsudku_23_Cdo_418_2026.pdf"
vse = {
    UNID.lower(): {"prvni": "2026-06-20T10:00:00+00:00", "title": "23 Cdo 418/2026",
                   "link": DETAIL_URL.format(UNID), "guid": UNID.lower(), "document-url": "https://x/418.pdf",
                   "category": "Náhrada škody", "ai-summary": "starší text", "ai-tag": "Škoda",
                   "pubDate": "Fri, 19 Jun 2026 12:00:00 +0000",
                   "description": "… (Usnesení 23 Cdo 418/2026, Heslo: Náhrada škody, rozhodnuto 2026-06-01, "
                                  "kategorie C)"},
    DESKA_PDF: {"prvni": "2026-06-10T08:00:00+00:00", "title": "23 Cdo 418 / 2026", "link": DESKA_PDF,
                "guid": DESKA_PDF, "ai-summary": SHRNUTI, "ai-tag": "Škoda",
                "pubDate": "Fri, 05 Jun 2026 12:00:00 +0000",
                "description": "… (Rozhodnutí 23 Cdo 418 / 2026, vyhlášeno 05.06.2026)"},
    "23 Cdo 3243 / 2024": {"prvni": "2026-03-30T06:00:00+00:00", "title": "23 Cdo 3243 / 2024",
                           "guid": "23 Cdo 3243 / 2024", "pubDate": "Mon, 30 Mar 2026 12:00:00 +0000",
                           "description": "Rozhodnutí 23 Cdo 3243 / 2024 vyhlášeno 27.03.2026"},
    "https://www.nsoud.cz/3243.pdf": {"prvni": "2026-04-02T06:00:00+00:00", "title": "23 Cdo 3243 / 2024",
                                      "link": "https://www.nsoud.cz/3243.pdf",
                                      "guid": "https://www.nsoud.cz/3243.pdf",
                                      "pubDate": "Mon, 30 Mar 2026 12:00:00 +0000"},
}
meta = {UNID: {"published": "2026-06-19", "decided": "2026-06-01", "heslo": "Náhrada škody",
               "typ": "USNESENÍ", "summary": SHRNUTI + " Z meta.", "tag": "Škoda"}}
seen = {UNID: "2026-06-17T15:35:52.969482+00:00"}
zs = {z["id"]: z for z in migrace.sestav(vse, meta, seen, TAX)}
check("tři záznamy: databáze, deska, deska bez odkazu sloučená",
      sorted(zs) == sorted([f"ns:{UNID}", "ns:deska:23cdo418/2026", "ns:deska:23cdo3243/2024"]),
      str(sorted(zs)))
db = zs[f"ns:{UNID}"]
check("databáze: data, druh, shrnutí z meta",
      db["druh"] == "usnesení" and db["datum"] == "2026-06-01" and db["zverejneno"] == "2026-06-19"
      and db["ai"]["shrnuti"] == SHRNUTI + " Z meta." and db["ai"]["zdroj"] == "migrace"
      and db["ai"]["pv"] == analyza.PROMPT_VERZE and db["ai"]["oblasti"] == [], str(db["ai"]))
check("první výskyt nejstarší z feed_seen, gitu a desky", db["first_seen"] == "2026-06-10T08:00:00Z",
      db["first_seen"])
d418 = zs["ns:deska:23cdo418/2026"]
check("deska nahrazená databází, rozsudek vyhlášený 5. 6.",
      d418["nahrazeno"] == f"ns:{UNID}" and d418["druh"] == "rozsudek" and d418["datum"] == "2026-06-05")
d3243 = zs["ns:deska:23cdo3243/2024"]
check("deska bez odkazu se sloučí s pozdější položkou s PDF",
      d3243["first_seen"] == "2026-03-30T06:00:00Z" and d3243["pdf"] == "https://www.nsoud.cz/3243.pdf"
      and d3243["datum"] == "2026-03-27" and d3243["nahrazeno"] is None, str(d3243))
fc.ai_volani = falesna_ai(lambda parts: json.dumps(
    [{"id": i, "oblasti": ["skoda"], "procesni": False} for i in (f"ns:{UNID}", "ns:deska:23cdo418/2026")]))
check("klasifikace jen aktivních záznamů se shrnutím", migrace.klasifikuj(list(zs.values()), TAX) == 1
      and db["ai"]["oblasti"] == ["skoda"] and "ns:deska:23cdo418/2026" not in volani[-1]["parts"][0]["text"])

# =====================================================================
print("\n10) Kontrola dat")
# =====================================================================
KD, KW = tmpdir(), tmpdir()
k = Sklad("ns", data_dir=KD, web_dir=KW).nacti(NYNI)
k.pridej(zaznam("ns:A", "2026-09-20T10:00:00Z"))
k.pridej(zaznam("ns:B", "2026-09-21T10:00:00Z", nahrazeno="ns:A"))
k.uloz()
k.exportuj(NYNI, 14)
check("čistý archiv projde", kontrola.zkontroluj(("ns",), KD, KW) == ([], []))
with open(os.path.join(KW, "ns.json"), encoding="utf-8") as f:
    okno = json.load(f)
okno["polozky"].append(dict(okno["polozky"][0], id="ns:B"))
with open(os.path.join(KW, "ns.json"), "w", encoding="utf-8") as f:
    json.dump(okno, f)
chyby, _ = kontrola.zkontroluj(("ns",), KD, KW)
check("nahrazený záznam v okně je chyba", any("nahrazený" in c for c in chyby), str(chyby))
with open(os.path.join(KD, "ns", "2026-09.jsonl"), "a", encoding="utf-8") as f:
    f.write("{rozbité\n")
chyby, _ = kontrola.zkontroluj(("ns",), KD, KW)
check("nečitelný řádek je chyba", any("nečitelný JSON" in c for c in chyby), str(chyby))
with open(os.path.join(KD, "ns", "index.tsv"), "w", encoding="utf-8") as f:
    f.write("ns:A\t2026-09\n")
_, varovani = kontrola.zkontroluj(("ns",), KD, KW)
check("díra v indexu je jen varování", any("v indexu chybí" in v for v in varovani), str(varovani))
chyby, varovani = kontrola.zkontroluj()
check("data v repu projdou kontrolou", not chyby, str(chyby)[:300])

# =====================================================================
fc.ai_volani = puvodni_ai
for d in DOCASNE:
    shutil.rmtree(d, ignore_errors=True)
failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} testů prošlo")
if failed:
    print("Neprošlo:")
    for n in failed:
        print("  -", n)
sys.exit(1 if failed else 0)
