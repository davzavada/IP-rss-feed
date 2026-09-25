# Owl – přehled novinek v doktríně a judikatuře

Statická stránka ([owl.davidzavada.cz](https://owl.davidzavada.cz/)),
kterou plní scrapery z GitHub Actions. Sleduje novou judikaturu Nejvyššího
soudu (všechny senáty), Nejvyššího správního soudu, Ústavního soudu a Soudního
dvora EU včetně Tribunálu (AI ji řadí do oblastí práva), články z právních
časopisů a nařízená jednání IP senátů Městského a Vrchního soudu v Praze. Ke všemu dělá AI (Gemini API) heslo
a třívěté shrnutí, jednou týdně z toho napíše dvoutýdenní přehled duševního
vlastnictví a IT.

Web má stránky Novinky (co přibylo za posledních 24 hodin, tedy úlovek
nočního běhu), Dva týdny v IP a IT (přehled je jeden pro všechny, na výběru
nezávisí), každý zdroj zvlášť (NS, NSS, ÚS, SDEU, časopisy), Kalendář
jednání a Kalendář akcí; Můj výběr je dialog z nabídky účtu.

## Jak to drží pohromadě

```
scraper_judikatura.py judikatura NS, NSS, ÚS a SDEU                 -> data/judikatura/, docs/data/judikatura/
scraper_journals.py  časopisy (weby, OJS, Crossref, RSS vydavatelů)  -> data/casopisy/, docs/data/casopisy.json
scraper_hearings.py  jednání MSPH a VS Praha (.docx/.pdf na justice) -> docs/hearings.json, hearings.ics
scraper_akce.py      vzdělávací akce pořadatelů (akce_config.json)  -> docs/akce.json, akce.ics
digest.py            dvoutýdenní přehled IP a IT (judikatura z oblastí IP/IT, časopisy) -> docs/digest.json
judikatura/          archiv, oblasti, mapy metadat, AI rozbor, fronta, adaptéry soudů (soudy/), migrace, kontrola
feed_common.py       sdílené: první výskyt položek, AI klient, prompty, cache shrnutí
docs/                stránka (index.html, style.css, app.js) a všechno, co čte
tools/probe_zdroje.py sonda: syrové odpovědi webů soudů pro parsery a testy
```

Časopisy si vedou **stav prvního výskytu** (`journals_seen.json`): kdy
položku poprvé viděly. Podle něj drží položku v okně (měsíc)
a web ji ukáže v Novinkách, když přibyla v posledních 24 hodinách (judikatura
totéž dělá přes `first_seen` v archivu). Registr časopisů (`CASOPISY`
ve `scraper_journals.py`) dává každému stálé id, které se ukládá ve výběru
uživatele, a zkratku pro štítek; okno `docs/data/casopisy.json` se
přepisuje, jen když se obsah změní. Stav prvního výskytu se u časopisů
neprořezává (zdroje vypisují i rok staré články, po vypadnutí ze stavu by se
vrátily jako nové); cache shrnutí `journals_meta.json` drží jen 120 dní.

Registr se do `casopisy.json` zapisuje při běhu scraperu; nový časopis
v registru je proto potřeba do souboru propsat hned (jinak ho dialog Můj
výběr ukáže až po nočním běhu). Časopisy na OJS (RPT, MUJLT, ČPVP, JIPITEC)
se čtou z RSS OJS. Časopisy s vlastním feedem vydavatele (JWIP, JIPLP a `DALSI_FEEDY`: IJLIT,
JPIL, CMLRev, ELJ) se čtou z feedu (RSS 2.0, RSS 1.0 i Atom) a když nevyjde,
z Crossrefu podle ISSN. Z GitHub Actions projde feed OUP (IJLIT) a Kluweru
(CMLRev); Wiley (ELJ) a Taylor & Francis (JPIL) vracejí 403, ty jedou přes
Crossref. Kluwer nedává DOI ani autory a místo data článku čas sestavení
feedu – guid je proto z kódu článku (COLA2026072, přežije přechod
z „[pre-publication]“ do čísla) a datum z prvního výskytu.

**Archiv časopisů** je v `data/casopisy/RRRR-MM.jsonl` podle měsíce prvního
výskytu: každý článek a číslo, co kdy prošlo oknem, jeden záznam (stejný jako
v okně pro web, se shrnutím) na řádek, seřazený podle id. Záznam se přepíše
novější verzí (i shrnutí). Web archiv nevidí. Starší
články (od března 2026) jsou do něj doplněné jednorázově z historie
`docs/journals_feed.xml` a `casopisy.json` v gitu. Archivy judikatury
i časopisů se neořezávají – drží se všechno.

RSS feedy web už nevydává – všechno je
na stránce (kalendář jednání dál i jako `hearings.ics`).
Tím nezáleží na tom, kdy zdroj položku datuje ani jestli datum později přepíše.

**AI** běží na free tieru Gemini API (klíč z Google AI Studia, projekt bez
billingu): nejdřív `gemini-flash-lite-latest`, a když nemůže (limit, výpadek),
nejnovější Gemma ze seznamu modelů. Pro a Flash se nepoužívají – Pro má na
free tieru kvótu vyčerpanou hned a Flash bývá přetížený. Alias `-latest`
posouvá Google sám, takže nový model se použije bez zásahu. Po vyčerpaném
denním limitu jde běh na další model. Přetížený model (5xx, timeout,
minutový limit na třech položkách po sobě) dostane desetiminutovou pauzu;
když ji mají všechny, běh na první z nich počká. Do konce běhu je model
pryč až po třetí pauze. Rozhodnutí, které nedostalo shrnutí jen kvůli
přetížení, si pokus nepočítá. Pořadí jde vnutit proměnnou `GEMINI_MODELS`
(názvy oddělené čárkou). Modelům jdou celé texty rozhodnutí, bez ořezu.

**AI shrnutí** se cachují v `*_meta.json` podle stejného klíče a prořezávají
se spolu se stavem prvního výskytu, takže soubory nerostou donekonečna. Bez
`GEMINI_API_KEY` scrapery běží dál, jen bez nových shrnutí. Když se k textu
nedostaneme vůbec (vydavatel stránku nepustil), nevymýšlí se nic a místo
shrnutí jde do feedu poznámka; dokud je položka v okně, zkouší se to každým
během znovu. Poznámka mluví jen za nás („shrnutí zatím není"), ne za zdroj.

## Judikatura

`scraper_judikatura.py` sbírá nová rozhodnutí soudů přes adaptéry
v `judikatura/soudy/` (Nejvyšší soud, Nejvyšší správní soud, Ústavní soud,
Soudní dvůr EU).
Adaptér umí tři věci: `objev(od, do)` najde rozhodnutí zveřejněná v tom
období, `doplnit(z)` přidá metadata z detailu a `text(z)` vrátí celý text
pro AI.

- **Archiv** je v `data/judikatura/{soud}/RRRR-MM.jsonl`: jeden záznam na
  řádek, seřazený podle id, v měsíci prvního výskytu. `index.tsv` drží
  všechna id, takže se nic nezdvojí ani po letech. Na web jde jen okno
  `docs/data/judikatura/{soud}.json` (u všech soudů i časopisů měsíc:
  `OKNO_DNI` = 31 dní ve `feed_common.py`, ať je první den měsíce v okně celý
  předchozí měsíc jako podklad pro měsíční shrnutí), a to jen když se obsah
  opravdu změní. Vercel archiv nevidí, nasazuje jen `docs/`.
- **První výskyt** je čas, kdy jsme rozhodnutí objevili. Když ale bylo
  zveřejněné před víc než třemi dny (vynechané běhy, první běh soudu), bere
  se datum zveřejnění, ať se staré netváří jako nové. Úplně první běh soudu
  hledá týden zpět, další deset dní.
- **AI rozbor** dělá jedním voláním heslo, nejvýš třívěté shrnutí, 1–3
  oblasti ze seznamu `docs/data/oblasti.json` a příznak čistě procesního
  rozhodnutí. Heslo je nadpis na jeden řádek, obecný právní závěr jako
  krátká právní věta (Postoupení autorských práv spadá pod Řím I), bez
  výsledku řízení a nikdy jen procesní institut („Přípustnost dovolání“); takové heslo (`heslo_obecne`) i hesla
  podle staršího pokynu (`ai.hv` < `HESLO_VERZE`) přepíše každý běh levně
  ze shrnutí, po dávkách (`prepis_hesel`, nejvýš 15 × 20 za běh). Model dostane vždy celý text a úřední údaje (heslo NS, oblast
  úpravy NSS, věcný rejstřík ÚS, dotčené předpisy) jako vodítko. Oblasti
  mimo seznam se zahodí.
- **První zařazení podle údajů soudu** (`oblasti_meta`, `judikatura/mapy.py`
  a `data/judikatura/mapy/`): oblast úpravy NSS, napadený správní orgán
  (ÚPV, ÚOOÚ, ČTÚ…), dotčené předpisy a věcný rejstřík ÚS. Platí, než
  rozhodnutí projde AI – řídí pořadí ve frontě a výběr na webu – a zůstane,
  když AI žádnou oblast nevrátí. Obecné kodexy (o. s. ř., s. ř. s.,
  občanský zákoník, Listina) v mapě předpisů chybí schválně.
- **Fronta**: AI zpracovává jen rozhodnutí z okna webu, střídavě po soudech.
  Nejdřív to, co spadá do výchozího výběru (senát 23, oblasti IP a IT), pak
  věcná a nakonec procesní rozhodnutí. Když text zatím není, zkouší se znovu
  po 1, 2, 4… hodinách, nejvýš šestkrát. Běh má rozpočet (`AI_MAX_POLOZEK`,
  `AI_MAX_MINUT`) a archiv ukládá po každém rozhodnutí.
- **Stav shrnutí**: rozhodnutí bez shrnutí má v okně `stav_shrnuti`
  (`pripravuje` – čeká ve frontě, `ceka_na_text` – soud ještě nezveřejnil
  text, `nepodarilo` – vyčerpané pokusy) a větu k němu v `poznamka`. Rámeček
  nad seznamem ukazuje, kolik rozhodnutí z celého okna ještě čeká na AI
  a jestli se do výběru podle oblastí dostanou až se zařazením (NS mimo
  vybrané senáty), nebo v něm už jsou podle údajů soudu (NSS, ÚS).
- **Nejvyšší soud**: databáze (Lotus Domino) padá na 500, když je dotaz moc
  široký. Hledá se proto po rejstřících (Cdo, NSČR, Tdo…), každý dotaz
  s čerstvou relací; co spadne i napodruhé, rozdělí se po senátech. Text se
  bere ze stránky rozhodnutí, pak z PDF (pypdf), a když PDF nemá textovou
  vrstvu, jde modelu PDF celé. Úřední deska ohlašuje vyhlášené rozsudky
  dřív, než je databáze zveřejní. Když pak přijde záznam z databáze se
  stejnou spisovou značkou, převezme od desky první výskyt i shrnutí
  a deska se na webu schová.
- **Nejvyšší správní soud** (vyhledavac.nssoud.cz): formulář ASP.NET
  s antiforgery tokenem – GET úvodní stránky, POST s „Datum zpřístupnění"
  od–do (do je půlnoc, zadává se den navíc), první stránka má 40 řádků,
  další po 20 dočítá POST na `/Home/MyResTRowsCont` s parametry, které
  stránka vypíše do skriptu. Vyhledávač drží i krajské soudy, bereme jen
  senáty NSS. Detail dá datum zpřístupnění, oblast úpravy, výrok, soudce,
  předpisy a napadený orgán; text je prostý text (UTF-16), záloha čitelná
  podoba a PDF originálu. Rozhodnutí vydané víc než rok před zpřístupněním
  (NSS starší dokumenty znovu zpřístupňuje po opravě) se za novinku
  nepovažuje. Procesní podle výroku (odmítnuto, zastaveno, odkladný účinek…).
- **Ústavní soud** (NALUS): WebForms s viewstate – GET formuláře, POST
  s datem zpřístupnění a řazením podle něj, další stránky `Results.aspx?page=N`
  v téže relaci. Výpis nese vše (značku, ECLI, soudce zpravodaje, populární
  název, data, předpisy, formu, výroky, předmět řízení, věcný rejstřík),
  text je na trvalé adrese `GetText.aspx?sz=…`. Procesní je odmítnutí podle
  § 43 odst. 1 (vady, lhůta, nepřípustnost…), ne pro zjevnou neopodstatněnost.
- **Soudní dvůr EU** (Soudní dvůr i Tribunál): objevování přes SPARQL
  Cellaru – rozsudky, usnesení a stanoviska generálních advokátů podle data
  dokumentu (abstrakty a výtahy ne) a nové předběžné otázky z oznámení
  v Úředním věstníku (CELEX typ CN, podle dne vložení do Cellaru; vyjdou
  dva až tři měsíce po podání, ale s otázkami). Žaloby a kasační opravné
  prostředky se neberou. Název věci a české texty dává InfoCuria podle čísla
  věci; kde český text ještě není (čerstvé rozsudky, Tribunál), bere se
  z Cellaru česky, anglicky, nebo francouzsky. Odkaz vede na EUR-Lex. Okno
  webu je měsíc.
  Doplňkově **ipcuria.eu** (`judikatura/soudy/ipcuria.py`): předběžné otázky
  z duševního vlastnictví a ochrany údajů podané za poslední měsíc, tedy
  dva až tři měsíce před oznámením v ÚV. Záznam `sdeu:ipc:{věc}` má datum
  podání, oblasti podle kategorií webu (`data/judikatura/mapy/ipcuria.json`)
  a odkaz na věc na webu Soudního dvora. Text je žádost o rozhodnutí
  o předběžné otázce z InfoCurie, pak oznámení v Cellaru, pak otázky ze
  stránky ipcuria; do té doby „Podáno {datum}. Položené otázky zatím nejsou
  zveřejněné.“ (datum podání je u předběžné otázky bez shrnutí vždy).
  Oznámení v ÚV ranou otázku převezme stejně jako databáze NS úřední desku:
  hotové shrnutí přejde na oznámení, bez shrnutí se oznámení ukáže jako nové.
- Na text rozhodnutí se čeká, dokud je rozhodnutí v okně – zkouší se při
  každém běhu. Šest pokusů mají jen selhání AI.
- **Stav běhu** (zdraví soudů, spotřeba AI po dnech) je v
  `data/judikatura/stav.json`. `python -m judikatura.kontrola` zkontroluje
  archiv i okna. Workflow bez ní necommituje.
- **Migrace**: `python -m judikatura.migrace` převedla shrnutí senátu 23 Cdo
  ze starého feedu (historie `docs/feed.xml` v gitu, `feed_meta.json`
  a `feed_seen.json`) do archivu a oblasti doplnila dávkově.

**Kalendář jednání** filtruje přehledy soudů podle `hearings_config.json`:
v civilním úseku na IP senáty (seznam senátů a soudců z rozvrhů práce), v úseku
správního soudnictví MSPH (zvláštní dokument na téže stránce) na žaloby
proti Úřadu průmyslového vlastnictví – podle žalovaného mezi účastníky
(`ucastnici_ip`), ne podle senátu. Každý nový přehled porovnává s minulým
a změny ukládá vedle jednání (na webu jsou v detailu jednání). Účastníky, kteří jsou fyzická osoba, drží
archiv jen pod iniciálami; kdo je fyzická osoba, rozhoduje AI, a ptá se jí
po dávkách, ať se odpověď vejde do stropu i s rostoucím archivem. U jména,
kde AI nerozhodne, se celé jméno neuloží; jednání si v takovém případě nechá
účastníky, které mu archiv přiřadil dřív, a úplně nové zůstane jen pod
spisovou značkou, dokud ho některý běh neklasifikuje. Bez toho by jeden
výpadek AI pokaždé shodil jinou část kalendáře zpátky na holé značky.

Senáty a jejich sestavy (předseda, členové, agenda, poznámka ke stážím) jsou
v configu sepsané ručně podle rozvrhu v `rozvrh_zdroj` (`platnost`,
`platnost_od`) a do `hearings.json` jdou i se `sestavy_navic` (správní 15 A
a 18 A MSPH, podle kterých se nefiltruje); web je nezobrazuje. Jednou týdně scraper stáhne
rozvrh ze stránky soudu a AI extrakcí seznam přepíše jen tehdy, když rozvrh
podle titulní strany platí od pozdějšího dne než ten zapsaný – starší ani
stejný dokument ruční seznam nepřepíše. Senáty ze sloupce „Zastupuje senát“
IP senáty jen zastupují a nesledují se.

**Kalendář akcí** (`scraper_akce.py`) sbírá semináře, webináře a konference
pořadatelů z `akce_config.json` (ČAK, PF UK, Jednota českých právníků,
epravo.cz, ALAI, ÚPV). U každého je výpis akcí, domény, na
které smí vést odkaz na přihlášku, zkratka a barva pro štítek; pořadí je
pořadí štítků na webu. Akce z výpisu se berou první cestou, která něco vrátí:
vlastní parser (`parser` v configu, `PARSERY` – zatím ČAK, jehož výpis je
tabulka), odkaz na iCal nebo schema.org Event v JSON-LD, a nakonec AI z textu
stránky (odkazy v něm zůstanou, ať AI vrátí i adresu akce; výzvy, granty
a studijní nabídky vynechá). Akcím, kterým ve výpisu chybí anotace, lektoři
nebo cena, se stáhne jejich stránka, u PDF (ÚPV) text z PDF a když stránka
odkazuje na pozvánku (ČAK), i ta. Rozpočet `AKCE_MAX_DETAILU` (80 za běh) se
dělí mezi pořadatele; na co nezbude, přijde na řadu další noc, nejvýš dva
pokusy na akci (`detail_pokusy`). Předpony formy v názvu („HYBRIDNÍ FORMA:",
„Online seminář:") jdou do pole `forma`. AI pak každou akci zařadí do 1–3
oblastí z `docs/data/oblasti.json`, stejně jako judikaturu; znovu se ptá,
jen když se změní název nebo anotace.

Na webu je Kalendář akcí stránkou vedle Kalendáře jednání (`#akce`): mřížka
na tři týdny (víkend jen když na něj akce připadá) s bublinou detailu,
seznam nadcházejících akcí, štítky pořadatelů v jejich barvě jako filtr
a přepínač „Moje oblasti / Vše“ podle oblastí z Můj výběr. Vícedenní akce je
v každém svém dni, kurz delší než týden jen v den začátku.

- Výstup `docs/akce.json`: `poradatele` (název, zkratka, barva, výpis, počet
  nadcházejících akcí a stav posledního čtení – `stazeno`, `cesta` = parser /
  ical / jsonld / ai, `chyba`), `formy` (popisky forem) a `akce` – každá
  s `id`, `poradatel`, `datum` (+ `datum_do` u vícedenních), `zacatek`,
  `konec`, `nazev`, `misto`, `forma` (`prezencne` / `online` / `hybridne`),
  `lektori`, `cena`, `anotace`, `url` a `oblasti`. Vedle je `akce.ics`
  k odběru v kalendáři (UID podle `id`).
- Výpis, který se nepodaří stáhnout, nechá akce pořadatele, jak byly. Budoucí
  akce, která z výpisu zmizí, vypadne (zrušená) – kromě případu, kdy výpis
  četla AI a vrátila míň než polovinu akcí proti minulému běhu; to se bere
  jako výpadek čtení. Proběhlé akce se drží 45 dní.
- Weby pořadatelů nejsou z vývojového prostředí vidět. Sonda je stáhne
  (`probe.yml` se zdrojem `akce`, s volbou `ulozit` do `tests/fixtures/probe/`)
  a podle nich jde pro web, kde AI čte špatně, napsat vlastní parser
  (výpis ČAK je v `tests/fixtures/akce/`).
- ALAI z GitHub Actions občas neodpovídá; pořadatel pak zůstane bez nových
  akcí (`chyba` v akce.json).

## Přihlášení a vlastní výběr

Přihlášení zajišťuje [Clerk](https://clerk.com) a slouží jen k vlastnímu
výběru. Bez přihlášení (i při výpadku Clerku) web ukazuje výchozí výběr:
u Nejvyššího soudu senát 23 a z ostatních senátů oblasti duševního
vlastnictví a IT, všechny časopisy. Nepřihlášenému to web říká pod
seznamy judikatury a nabízí přihlášení (při výpadku Clerku ne).

- Web je bez buildu, takže Clerk se načítá skriptem z Frontend API instance
  (`@clerk/clerk-js@6` a komponenty `@clerk/ui@1`), až po vykreslení obsahu.
  Česká lokalizace je v `docs/vendor/clerk-cs-CZ.js`.
- Publishable key je veřejný a je v `docs/app.js` (`CLERK_KLICE`, podle
  hostitele: produkční instance pro `owl.davidzavada.cz`, jinak vývojová).
  Tajný klíč web nepotřebuje a do kódu ani na Vercel nepatří (přehled je
  jeden pro všechny, výběry uživatelů se nikde nečtou).
- Výběr je u účtu v `user.unsafeMetadata.owl`:
  `{"v":1,"ns":{"oblasti":[…],"senaty":[23]},"nss":{"oblasti":[…]},"us":{…},"sdeu":{…},"skryt_procesni":false,"skryte_casopisy":[]}`.
  Nastavuje se v dialogu Můj výběr, který se otevírá z nabídky účtu
  (tlačítko Clerku v hlavičce, první položka) nebo z odkazu „Upravit výběr“
  u prázdného seznamu; stará kotva `#nastaveni` ho otevře taky. Po registraci
  se otevře sám (čerstvý účet bez uloženého výběru, jednou – příznak
  `owl:uvitani:{id}` v localStorage) s jednou uvítací větou nahoře. Vlevo
  jsou záložky oblasti práva, senáty NS a časopisy (na telefonu přepínač
  nahoře) s krátkým souhrnem („5 z 25", „senát 23", „všech 13"); skupiny
  ukazují počet vybraných a „Vybrat vše / Zrušit vše", vybírá se
  zaškrtávacími poli. Oblasti se zatím nastavují
  všem soudům stejně (výběr po soudech je v datech připravený, na stránce
  schovaný). Občanskoprávní senáty jsou jednotlivě, trestní kolegium jedním
  vypínačem. Změny platí až po tlačítku Uložit (pak se dialog zavře);
  Zrušit změny zahodí; zavřít křížkem nebo Esc s neuloženými změnami jde
  po potvrzení (a jen tehdy hlídá stránku
  `beforeunload`). Neznámé oblasti se zahodí,
  přejmenované převede `alias` v `docs/data/oblasti.json`; soud, který
  v uloženém výběru chybí, dostane výchozí oblasti. Výchozí výběr se
  neukládá. Procesní rozhodnutí se neskrývají (dřívější `skryt_procesni`
  se ignoruje).
- Pravidlo: rozhodnutí je vidět, když spadá do některé z oblastí vybraných
  u jeho soudu; u NS navíc všechna rozhodnutí vybraných senátů.

## Workflow

- `update-feed.yml` – časopisy, kalendář jednání a akce jednou denně ve 2:00
  pražského času, v pondělí k tomu `digest.py`. Cron má dva výrazy (0:00
  a 1:00 UTC) a krok „Naplánovat běh" pustí ten, který v daném čase roku
  odpovídá 2:00 v Praze. Každý scraper je samostatný krok. Když jeden
  spadne, ostatní doběhnou a commit uloží, co se povedlo.
- `judikatura.yml` – sběr judikatury taky jednou denně ve 2:00 (soudy
  zveřejňují přes den, ráno je hotovo všechno z předchozího dne). Jediný
  běh má na AI rozpočet až 300 rozhodnutí a 90 minut. Ručně jde pustit
  kdykoli, jen pro vybrané soudy, bez AI nebo s jiným rozpočtem.
- `probe.yml` – jen ručně: stáhne odpovědi webů soudů (formuláře, výpisy,
  detaily, InfoCuria, SPARQL) jako artefakt, s volbou `ulozit` je commitne
  do vybrané větve jako fixtures. Na weby soudů je vidět jen z Actions.
  U každé odpovědi zapíše i `Content-Disposition` (jestli prohlížeč PDF
  ukáže, nebo stáhne); se soudy `-` stáhne jen zadané adresy.
- `tests.yml` – `test_hearings.py` a `test_journals.py` nad uloženými
  originály dokumentů v `tests/fixtures`, `test_judikatura.py` (archiv, fronta,
  AI rozbor, adaptéry NS, NSS a ÚS nad uloženými odpověďmi soudů, mapy metadat,
  migrace, kontrola dat v repu), `test_ai.py`, `test_digest.py`,
  `test_akce.py` (čtení JSON-LD, iCal a AI nad syntetickými stránkami, sloučení,
  oblasti, akce.ics) a `tests/test_pdf_api.js` (náhled PDF, v Node).

## Lokálně

```
pip install -r requirements.txt icalendar   # icalendar jen pro testy
python test_hearings.py && python test_journals.py && python test_judikatura.py && python test_akce.py
node --test tests/test_pdf_api.js           # náhled PDF (api/pdf.js)
python scraper_journals.py                  # a další scrapery stejně
SKIP_GEMINI=1 python scraper_journals.py    # bez AI
SKIP_GEMINI=1 python scraper_judikatura.py --soudy ns   # jen objevování
python scraper_hearings.py --local-jednani MS=tests/fixtures/msph_civilni_2026-08-16_31.docx
SKIP_GEMINI=1 python scraper_akce.py --local UPV=akce.ics   # akce z uložené stránky / .ics
```

Stránku stačí otevřít přes libovolný statický server nad `docs/`
(`python -m http.server -d docs`), čte soubory vedle sebe.

## Nasazení

Stránku servíruje Vercel: projekt napojený na tohle repo, bez build kroku,
výstupem je adresář `docs/` (viz `vercel.json`). Nasazuje se jen commit,
který změní `docs/`, `api/` nebo `vercel.json` (`ignoreCommand`) – commity se
stavem scraperů mimo `docs/` deploy nespouštějí.

Jediná funkce na serveru je náhled PDF (`api/pdf.js`). Vyhledávač NSS
posílá PDF rozhodnutí s `Content-Disposition: attachment`, takže se po
kliknutí rovnou stáhne. Odkaz „PDF" u rozhodnutí NSS proto vede na
`/pdf/nss/{id}/{spisová značka}.pdf` (přepis ve `vercel.json`); funkce
PDF z NSS stáhne a pošle ho jako `inline`, prohlížeč ho otevře v nové
kartě. Bere jen číselné id dokumentu, adresu na NSS si skládá sama. Když
NSS neodpoví nebo nevrátí PDF (nebo je PDF větší než 4 MB, víc funkce
vrátit nesmí), přesměruje na původní adresu. CDN Vercelu si PDF drží
týden. PDF Nejvyššího soudu jdou rovnou na soud, ten je posílá inline sám.
Lokálně (bez Vercelu) vede odkaz rovnou na NSS. Doménu (`owl.davidzavada.cz`)
nese záznam CNAME u správce DNS, nasměrovaný na Vercel; GitHub Pages je
vypnuté. Doména webu pro odkaz na kalendář jde přepsat proměnnou
`SITE_HOST`. UID událostí v `hearings.ics` drží doménu `rss.davidzavada.cz`
z doby před přesunem, ať kalendáře nevidí jednání dvakrát.
