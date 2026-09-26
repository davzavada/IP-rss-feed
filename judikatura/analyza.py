"""AI rozbor rozhodnutí: heslo, shrnutí, oblasti a příznak procesní.

Jedno volání na rozhodnutí, vždy nad celým textem. Modely Gemini dostanou
JSON schéma (oblasti jako výčet povolených id), Gemma stejné pokyny
a odpovídá značkami HESLO / SHRNUTÍ / OBLASTI / PROCESNÍ. Parser zvládne
obojí; oblasti mimo seznam zahodí.
"""

import base64
import json
import re

import feed_common as fc
from judikatura.model import NAZVY_SOUDU, bez_diakritiky

# Zvýšit, když se změní prompt tak, že stará shrnutí už neodpovídají –
# rozhodnutí v okně se pak přepočítají (archiv se sám nepřepisuje).
PROMPT_VERZE = 1
MIN_SHRNUTI = 40
# Verze pokynu k heslu. Heslo se dá přepsat levně ze shrnutí (prepis_hesel),
# takže změna hesla nevyvolá nový rozbor celých textů jako PROMPT_VERZE.
HESLO_VERZE = 4
HESLO_MAX_SLOV = 9
HESLO_MAX_ZNAKU = 75

# Pokyn k heslu – sdílí ho rozbor i přepis starých hesel.
HESLO_POKYN = (
    "HESLO – nadpis na jeden řádek, čtyři až šest slov, nejvýš osm: obecný "
    "právní závěr, který z rozhodnutí plyne a platí i pro jiné spory, "
    "stručně jako právní věta. Pomlčku ani dvojtečku nepotřebuje; použij "
    "je, jen když nadpis zkrátí. Příklady: Postoupení autorských práv "
    "spadá pod Řím I; Obecné ujištění nevylučuje zjevné vady; "
    "Nezpůsobilost jednat lze prokázat posudkem; Prodloužení povolení "
    "vyžaduje účast veřejnosti; Spolku stačí tvrdit zásah do práv; Stání "
    "vozidla lze prokázat statickými snímky; Předem zaslaná nabídka: "
    "smlouva mimo obchodní prostory. Nepiš výsledek řízení (dovolání "
    "odmítnuto, nepřípustné, zamítnuto, zrušeno, vyhověno, nepřiznán) ani "
    "jména a místa z případu. Když rozhodnutí obecný závěr nemá (odmítnutí "
    "bez věcného posouzení, zastavení, příslušnost, předběžná otázka bez "
    "odpovědi), napiš jen právní otázku: Bezdůvodné obohacení a právní "
    "důvod plnění. Nikdy nepiš jen procesní institut (Přípustnost "
    "dovolání, Odmítnutí ústavní stížnosti, Zastavení řízení, Místní "
    "příslušnost, Odkladný účinek, Podjatost, Náklady řízení).\n"
)

# Slova procesních institutů. Heslo složené jen z nich (Přípustnost
# dovolání, Místní příslušnost soudu, Zastavení dovolacího řízení) nic
# neříká o věci – takové se přepíše, i když ho model vrátil. Porovnává se
# bez diakritiky a velikosti písmen.
PROCESNI_SLOVA = set("""
a od pro v ve o na k z
pripustnost nepripustnost nepripustne pripustne prijatelnost neprijatelnost
odmitnuti odmitnuto zastaveni zastaveno zpetvzeti opozdena opozdene opozdenost
mistni vecna vecne prislusnost prislusnosti prislusny odkladny odkladneho ucinek ucinku
podjatost podjatosti namitka namitky naklady nakladu nahrada nahrady duvody duvod
spojeni veci vec obnova obnovy rizeni rizení hodnoceni dukazu dukazy
osvobozeni soudnich soudni poplatku poplatek poplatky prikazani
dovolani dovolaci dovolaciho dovolacim kasacni kasacniho stiznosti stiznost ustavni
soudu soud exekuci exekuce vady vad navrhu navrh lhuta lhuty zmeskani prominuti
ustanoveni zastupce preruseni bagatelni predbezne predbezneho opatreni
""".split())


# Slova výsledku řízení. Část hesla za pomlčkou (dvojtečkou) složená jen z nich
# („– dovolání odmítnuto", „– nepřípustné") je výsledek sporu, ne obecný
# právní závěr, a odřízne se (ocisti_heslo).
VYSLEDEK_SLOVA = PROCESNI_SLOVA | set("""
odmitnuto odmitnuta odmitnute odmitnut zamitnuto zamitnuta zamitnute zamitnut
zruseno zrusena zruseny zrusen vyhoveno vyhovena vyhoveni potvrzeno potvrzena
nepripustna nepripustny nepriznan nepriznano priznan priznano neprijatelna
neprijatelne zastavena prislusny prislusna urcen urcena nedovodna duvodna
castecne jen neuspesne uspesne
""".split())


def ocisti_heslo(heslo):
    """Odřízne z hesla výsledek řízení za pomlčkou („Předkupní právo
    k pozemku – dovolání odmítnuto" -> „Předkupní právo k pozemku")."""
    h = re.sub(r"\s+", " ", str(heslo or "")).strip().rstrip(".")
    # Oddělovače zůstávají v seznamu (sudé indexy = části), ať se vrátí,
    # jak byly.
    casti = re.split(r"(\s+[–—-]\s+|:\s+)", h)
    while len(casti) > 1:
        slova = [w for w in re.split(r"[^\w]+", bez_diakritiky(casti[-1]).lower()) if w]
        if slova and all(w in VYSLEDEK_SLOVA for w in slova):
            del casti[-2:]
        else:
            break
    return "".join(casti)


def heslo_obecne(heslo):
    """Heslo, ze kterého není poznat, o co ve věci jde (jen procesní
    institut), nebo je moc dlouhé na jeden řádek."""
    h = re.sub(r"\s+", " ", str(heslo or "")).strip().rstrip(".")
    slova = [w for w in re.split(r"[^\w]+", bez_diakritiky(h).lower()) if w]
    return (not slova or all(w in PROCESNI_SLOVA for w in slova)
            or len(h) > HESLO_MAX_ZNAKU or len(slova) > HESLO_MAX_SLOV)

SYSTEM = (
    "Jsi asistent českého advokáta. Dostaneš jedno soudní rozhodnutí (případně "
    "stanovisko generálního advokáta nebo žádost o rozhodnutí o předběžné "
    "otázce) i s úředními údaji. Odpovídej česky, i když je text v jiném "
    "jazyce. Nic si nevymýšlej, drž se textu.\n\n"
    "Vrať čtyři údaje:\n"
    + HESLO_POKYN +
    "SHRNUTÍ – nejvýše tři věty podle pokynu u rozhodnutí. Právnické osoby "
    "a úřady uváděj jménem, ale bez právní formy (bez s.r.o., a.s. apod.); "
    "fyzické osoby nejmenuj, piš žalobce, žalovaný, stěžovatel, obviněný.\n"
    "OBLASTI – jeden až tři identifikátory ze seznamu níže podle věcné "
    "podstaty sporu, ne podle procesního rámce; první je hlavní. Vždy vyber "
    "aspoň jednu – když žádná nesedí přesně, tu nejbližší.\n"
    "PROCESNÍ – ano jen tehdy, když rozhodnutí nemá věcný právní závěr "
    "(odmítnutí pro vady, opožděnost nebo nepřípustnost bez věcného "
    "posouzení, zastavení řízení, přikázání věci, příslušnost, podjatost, "
    "poplatky, ustanovení zástupce); jinak ne.\n\n"
    "Když dotaz chce JSON, použij pole heslo, shrnuti, oblasti a procesni. "
    "Jinak odpověz přesně takto, bez dalšího textu:\n"
    "HESLO: …\nSHRNUTÍ: …\nOBLASTI: id, id\nPROCESNÍ: ano/ne\n\n"
    "Seznam oblastí (id – název: co sem patří):\n{oblasti}"
)

POKYNY = {
    "obecny": "V první větě kdo se s kým soudil a o co šlo, pak jakou právní "
              "otázku soud řešil a jak ji vyřešil – konkrétní právní závěr.",
    "us": "Co stěžovatel napadal, zda Ústavní soud shledal porušení kterého "
          "základního práva a proč; u odmítnutí stručně důvod.",
    "sdeu": "Jakou otázku Soudní dvůr (Tribunál) řešil a jak ji zodpověděl – "
            "konkrétní závěr.",
    "stanovisko": "Jakou odpověď generální advokát navrhuje a proč.",
    "otazka": "Na co se předkládající soud (a ze které země) Soudního dvora ptá.",
    # Stanovisko kolegia nebo pléna NS (Cpjn, Tpjn, Plsn) – spor v něm není.
    "stanovisko_ns": "Jakou otázku, v níž se soudy rozcházely, kolegium (plénum) "
                     "Nejvyššího soudu sjednocovalo a jaký závěr přijalo.",
}


def pokyn(z):
    druh = (z.get("druh") or "").lower()
    if z["soud"] == "sdeu":
        if "stanovisko" in druh:
            return POKYNY["stanovisko"]
        if "otázk" in druh or "otazk" in druh:
            return POKYNY["otazka"]
        return POKYNY["sdeu"]
    if z["soud"] == "us":
        return POKYNY["us"]
    if z["soud"] == "ns" and ("stanovisko" in druh
                              or (z.get("rejstrik") or "").lower() in ("cpjn", "tpjn", "plsn")):
        return POKYNY["stanovisko_ns"]
    return POKYNY["obecny"]


def schema(tax):
    return {
        "type": "OBJECT",
        "properties": {
            "heslo": {"type": "STRING"},
            "shrnuti": {"type": "STRING"},
            "oblasti": {"type": "ARRAY", "items": {"type": "STRING", "enum": tax.ids}},
            "procesni": {"type": "BOOLEAN"},
        },
        "required": ["heslo", "shrnuti", "oblasti", "procesni"],
        "propertyOrdering": ["heslo", "shrnuti", "oblasti", "procesni"],
    }


def hlavicka(z):
    """Úvod dotazu: kdo, co, kdy a úřední údaje jako vodítko."""
    radky = [f"Soud: {NAZVY_SOUDU.get(z['soud'], z['soud'])}"]
    for popis, klic in (("Druh", "druh"), ("Spisová značka", "spz"), ("Název věci", "nazev"),
                        ("Datum rozhodnutí", "datum")):
        if z.get(klic):
            radky.append(f"{popis}: {z[klic]}")
    napoveda = [f"{k}: {v}" for k, v in (z.get("meta") or {}).items()
                if v and k not in ("soudce",) and isinstance(v, (str, int))]
    napoveda += [f"{k}: {', '.join(v)}" for k, v in (z.get("meta") or {}).items()
                 if isinstance(v, list) and v]
    if napoveda:
        radky.append("Úřední údaje (vodítko, nemusí být úplné): " + " | ".join(napoveda))
    radky.append(f"Pokyn ke shrnutí: {pokyn(z)}")
    return "\n".join(radky)


def dotaz(z, obsah):
    """Části dotazu: celý text, nebo PDF, když text není."""
    uvod = hlavicka(z)
    text = (obsah.get("text") or "").strip()
    if text:
        return [{"text": uvod + "\n\n--- TEXT ---\n" + text}]
    return [{"inline_data": {"mime_type": "application/pdf",
                             "data": base64.b64encode(obsah["pdf"]).decode("ascii")}},
            {"text": uvod + "\n\nText rozhodnutí je v přiloženém PDF."}]


def _json_z(raw):
    """Objekt JSON z odpovědi (i obalené ```json … ```); None, když tam není."""
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _ano(v):
    if isinstance(v, bool):
        return v
    s = str(v or "").strip().lower()
    if s.startswith(("ano", "yes", "true")):
        return True
    if s.startswith(("ne", "no", "false")):
        return False
    return None


def _cist(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().strip("*").strip()


def parse(raw, tax):
    """Odpověď modelu -> {heslo, shrnuti, oblasti, procesni}; None, když se
    nedá použít (chybí shrnutí nebo je podezřele krátké)."""
    raw = re.sub(r"\*\*|__", "", raw or "")
    data = _json_z(raw)
    if data is not None:
        heslo, shrnuti = data.get("heslo"), data.get("shrnuti") or data.get("shrnutí")
        oblasti, procesni = data.get("oblasti"), data.get("procesni", data.get("procesní"))
        if isinstance(oblasti, str):
            oblasti = re.split(r"[,;\n]+", oblasti)
    else:
        def cast(znacka, dalsi):
            m = re.search(rf"{znacka}\s*:\s*(.*?)\s*(?=(?:{dalsi})\s*:|$)", raw, re.I | re.S)
            return m.group(1) if m else ""
        vse = r"HESLO|SHRNUT[IÍ]|OBLASTI|PROCESN[IÍ]"
        heslo = cast("HESLO", vse)
        shrnuti = cast(r"SHRNUT[IÍ]", vse)
        oblasti = re.split(r"[,;\n]+", cast("OBLASTI", vse))
        procesni = cast(r"PROCESN[IÍ]", vse)
    shrnuti = fc.bez_pravni_formy(_cist(shrnuti))
    if len(shrnuti) < MIN_SHRNUTI:
        return None
    return {
        "heslo": ocisti_heslo(fc.bez_pravni_formy(_cist(heslo))),
        "shrnuti": shrnuti,
        "oblasti": tax.normalizuj([_cist(o) for o in oblasti or [] if _cist(o)]),
        "procesni": _ano(procesni),
    }


# Když AI nevrátí žádnou oblast ze seznamu a nepomohou ani úřední údaje:
# oblast, kam věci od toho soudu patří nejčastěji (u NS podle rejstříku –
# T… je trestní).
NAHRADNI_OBLAST = {"nss": "spravni", "us": "ustavni", "sdeu": "spravni"}


def nahradni_oblasti(z):
    if z.get("oblasti_meta"):
        return list(z["oblasti_meta"])
    if z["soud"] == "ns":
        return ["trestni" if (z.get("rejstrik") or "").startswith("T") else "civilni_proces"]
    return [NAHRADNI_OBLAST.get(z["soud"], "spravni")]


def analyzuj(z, obsah, tax):
    """Rozbor jednoho rozhodnutí. Vrací (výsledek, model) nebo (None, '')."""
    raw, model = fc.ai_volani(dotaz(z, obsah), system=SYSTEM.format(oblasti=tax.do_promptu()),
                              schema=schema(tax), max_tokens=4096,
                              timeout=fc.GEMINI_PDF_TIMEOUT if not obsah.get("text")
                              else fc.GEMINI_TEXT_TIMEOUT)
    if not raw:
        return None, ""
    vysledek = parse(raw, tax)
    if vysledek is None:
        print(f"    [diag] {z['id']}: odpověď AI se nedala použít: {raw[:120]!r}")
        return None, ""
    if not vysledek["oblasti"]:
        vysledek["oblasti"] = nahradni_oblasti(z)
    if vysledek["procesni"] is None:
        vysledek["procesni"] = bool(z.get("procesni_meta"))
    return vysledek, model


PREPIS_HESEL_SYSTEM = (
    "Jsi asistent českého advokáta. U každého rozhodnutí níže máš dosavadní "
    "heslo a shrnutí. Napiš ke každému nové heslo podle tohoto pokynu:\n"
    + HESLO_POKYN +
    "Vycházej jen ze shrnutí, nic si nevymýšlej; fyzické osoby nejmenuj, "
    "firmy bez právní formy. Odpověz jen JSON objektem {\"<id>\": \"heslo\", …} "
    "s klíči ze zadání."
)
PREPIS_DAVKA = 20


def prepis_hesla_davku(zaznamy):
    """Nová hesla ze shrnutí pro dávku rozhodnutí – jedno volání. Vrací
    {id: heslo} jen s hesly, která projdou (heslo_obecne); None, když AI
    neodpověděla (dávka se zkusí příště)."""
    text = "\n\n".join(
        f"id: {z['id']}\nsoud: {NAZVY_SOUDU.get(z['soud'], z['soud'])}"
        + (f" – {z['druh']}" if z.get("druh") else "")
        + f"\ndosavadní heslo: {(z.get('ai') or {}).get('heslo', '')}"
        + f"\nshrnutí: {(z.get('ai') or {}).get('shrnuti', '')}"
        for z in zaznamy)
    raw, _ = fc.ai_volani([{"text": text}], system=PREPIS_HESEL_SYSTEM, max_tokens=4096)
    data = _json_z(re.sub(r"\*\*|__", "", raw or ""))
    if data is None:
        return None
    platna = {z["id"] for z in zaznamy}
    out = {}
    for id_, heslo in data.items():
        heslo = ocisti_heslo(fc.bez_pravni_formy(_cist(heslo)))
        if id_ in platna and not heslo_obecne(heslo):
            out[id_] = heslo
    return out


KLASIFIKACE_SYSTEM = (
    "Jsi asistent českého advokáta. U každého rozhodnutí níže máš heslo "
    "a shrnutí. Zařaď každé do jedné až tří oblastí ze seznamu (podle věcné "
    "podstaty, první je hlavní) a urči, zda je čistě procesní (bez věcného "
    "právního závěru). Odpověz jen JSON polem objektů {{\"id\": …, \"oblasti\": "
    "[…], \"procesni\": true/false}} v pořadí zadání.\n\n"
    "Seznam oblastí (id – název: co sem patří):\n{oblasti}"
)


def klasifikuj_davku(zaznamy, tax):
    """Doplní oblasti a příznak procesní k hotovým shrnutím (migrace starých
    shrnutí) – jedno volání na dávku. Vrací {id: (oblasti, procesni)}."""
    text = "\n\n".join(
        f"id: {z['id']}\nsp. zn.: {z.get('spz', '')}\nheslo: {(z.get('ai') or {}).get('heslo', '')}\n"
        f"shrnutí: {(z.get('ai') or {}).get('shrnuti', '')}"
        + (f"\núřední heslo: {z['meta'].get('heslo_ns')}" if (z.get('meta') or {}).get('heslo_ns') else "")
        for z in zaznamy)
    raw, _ = fc.ai_volani([{"text": text}],
                          system=KLASIFIKACE_SYSTEM.format(oblasti=tax.do_promptu()),
                          max_tokens=8192)
    m = re.search(r"\[.*\]", re.sub(r"\*\*|__", "", raw or ""), re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return {}
    out = {}
    for polozka in data if isinstance(data, list) else []:
        if isinstance(polozka, dict) and polozka.get("id"):
            out[str(polozka["id"])] = (tax.normalizuj(polozka.get("oblasti") or []),
                                       _ano(polozka.get("procesni")))
    return out
