#!/usr/bin/env python3
"""Testy sběru judikatury (balíček judikatura/ a adaptér Nejvyššího soudu).

Bez sítě: web NS i Gemini API se simulují. Jde o to, co pipeline s daty
udělá – že se stejné rozhodnutí nevede dvakrát, že staré se netváří jako
nové, že archiv v gitu nemění řádky zbytečně, že AI dostane celý text
a že se výpadek jednoho zdroje nepropíše do ostatních.

Spuštění: python test_judikatura.py
"""

import base64
import json
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import feed_common as fc
from judikatura import analyza, fronta, kontrola, migrace, model, orchestr
from judikatura.sklad import Sklad, slim
from judikatura.soudy import ns
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
check("26 oblastí, id jedinečná", len(TAX.ids) == 26 and len(set(TAX.ids)) == 26, str(len(TAX.ids)))
check("výchozí výběr je IP/IT",
      TAX.vychozi == ["autorske", "prumyslova_prava", "nekala_soutez", "it", "gdpr"], str(TAX.vychozi))
check("neznámé id zahodí, název převede, nejvýš tři",
      TAX.normalizuj(["Autorské právo", "it", "neexistuje", "GDPR", "zavazky"]) == ["autorske", "it", "gdpr"],
      str(TAX.normalizuj(["Autorské právo", "it", "neexistuje", "GDPR", "zavazky"])))
check("„ostatni“ jen když nic jiného",
      TAX.normalizuj(["ostatni", "zavazky"]) == ["zavazky"] and TAX.normalizuj(["ostatni"]) == ["ostatni"])
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
s = slim(zaznam("ns:E", "2026-09-22T00:00:00Z", oblasti_meta=["dane"], procesni_meta=True,
                ai={"heslo": "H", "shrnuti": SHRNUTI, "oblasti": ["spravni"], "procesni": False}))
check("oblasti a procesní z AI mají přednost",
      s["oblasti"] == ["spravni"] and s["procesni"] is False and "poznamka" not in s, str(s))

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
check("odklad 1, 2, 4… hodin, nejvýš den", odklady == [1, 2, 4, 8, 16, 24, 24], str(odklady))
check("po šesti pokusech se to vzdá", not fronta.potrebuje_ai(z, NYNI + timedelta(days=2)))
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
print("\n8) Migrace starého feedu 23 Cdo")
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
print("\n9) Kontrola dat")
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
