# Owl – přehled novinek v IP a IT

Statická stránka ([owl.davidzavada.cz](https://owl.davidzavada.cz/)),
kterou plní scrapery z GitHub Actions. Sleduje novou judikaturu Nejvyššího
soudu (všechny senáty, AI ji řadí do oblastí práva), judikaturu Soudního
dvora EU k duševnímu vlastnictví a IT, články z právních časopisů a nařízená
jednání IP senátů Městského a Vrchního soudu v Praze. Ke všemu dělá AI (Gemini API) heslo
a třívěté shrnutí, jednou týdně z toho napíše dvoutýdenní přehled.

## Jak to drží pohromadě

```
scraper_judikatura.py judikatura NS (databáze + úřední deska)       -> data/judikatura/, docs/data/judikatura/
scraper_ipcuria.py   CJEU (ipcuria.eu, InfoCuria, EUR-Lex)           -> docs/ipcuria_feed.xml
scraper_journals.py  časopisy (weby, OJS, Crossref, RSS vydavatelů)  -> docs/journals_feed.xml
scraper_hearings.py  jednání MSPH a VS Praha (.docx/.pdf na justice) -> docs/hearings.json, hearings.ics
digest.py            dvoutýdenní přehled z judikatury a feedů výše   -> docs/digest.json
judikatura/          archiv, oblasti, AI rozbor, fronta, adaptéry soudů (soudy/), migrace, kontrola
feed_common.py       sdílené: první výskyt položek, AI klient, prompty, cache shrnutí
docs/                stránka (index.html, style.css, app.js) a všechno, co čte
tools/probe_zdroje.py sonda: syrové odpovědi webů soudů pro parsery a testy
```

Každý feed si vede **stav prvního výskytu** (`*_seen.json`): kdy položku
poprvé viděl. Podle něj drží položku v okně (časopisy čtyři týdny, CJEU
osm) a označuje ji jako novou, když přibyla v posledních 24 hodinách.
Tím nezáleží na tom, kdy zdroj položku datuje ani jestli datum později přepíše.

**AI** běží na free tieru Gemini API (klíč z Google AI Studia, projekt bez
billingu): nejdřív `gemini-flash-lite-latest`, a když nemůže (limit, výpadek),
nejnovější Gemma ze seznamu modelů. Pro a Flash se nepoužívají – Pro má na
free tieru kvótu vyčerpanou hned a Flash bývá přetížený. Alias `-latest`
posouvá Google sám, takže nový model se použije bez zásahu. Po vyčerpaném
denním limitu jde běh na další model. Pořadí jde vnutit proměnnou
`GEMINI_MODELS` (názvy oddělené čárkou). Modelům jdou celé texty rozhodnutí,
bez ořezu.

**AI shrnutí** se cachují v `*_meta.json` podle stejného klíče a prořezávají
se spolu se stavem prvního výskytu, takže soubory nerostou donekonečna. Bez
`GEMINI_API_KEY` scrapery běží dál, jen bez nových shrnutí. Když se k textu
nedostaneme vůbec (vydavatel stránku nepustil), nevymýšlí se nic a místo
shrnutí jde do feedu poznámka; dokud je položka v okně, zkouší se to každým
během znovu. Poznámka mluví jen za nás („shrnutí zatím není"), ne za zdroj.

## Judikatura

`scraper_judikatura.py` sbírá nová rozhodnutí soudů přes adaptéry
v `judikatura/soudy/` (zatím Nejvyšší soud; NSS, ÚS a SDEU přibudou).
Adaptér umí tři věci: `objev(od, do)` najde rozhodnutí zveřejněná v tom
období, `doplnit(z)` přidá metadata z detailu a `text(z)` vrátí celý text
pro AI.

- **Archiv** je v `data/judikatura/{soud}/RRRR-MM.jsonl`: jeden záznam na
  řádek, seřazený podle id, v měsíci prvního výskytu. `index.tsv` drží
  všechna id, takže se nic nezdvojí ani po letech. Na web jde jen okno
  `docs/data/judikatura/{soud}.json` (NS, NSS, ÚS 14 dní, SDEU 30), a to jen
  když se obsah opravdu změní. Vercel archiv nevidí, nasazuje jen `docs/`.
- **První výskyt** je čas, kdy jsme rozhodnutí objevili. Když ale bylo
  zveřejněné před víc než třemi dny (vynechané běhy, první běh soudu), bere
  se datum zveřejnění, ať se staré netváří jako nové. Úplně první běh soudu
  hledá týden zpět, další deset dní.
- **AI rozbor** dělá jedním voláním heslo, nejvýš třívěté shrnutí, 1–3
  oblasti ze seznamu `docs/data/oblasti.json` a příznak čistě procesního
  rozhodnutí. Model dostane vždy celý text a úřední údaje (heslo NS, dotčené
  předpisy) jako vodítko. Oblasti mimo seznam se zahodí.
- **Fronta**: AI zpracovává jen rozhodnutí z okna webu, střídavě po soudech.
  Nejdřív to, co spadá do výchozího výběru (senát 23, oblasti IP a IT), pak
  věcná a nakonec procesní rozhodnutí. Když text zatím není, zkouší se znovu
  po 1, 2, 4… hodinách, nejvýš šestkrát. Běh má rozpočet (`AI_MAX_POLOZEK`,
  `AI_MAX_MINUT`) a archiv ukládá po každém rozhodnutí.
- **Nejvyšší soud**: databáze (Lotus Domino) padá na 500, když je dotaz moc
  široký. Hledá se proto po rejstřících (Cdo, NSČR, Tdo…), každý dotaz
  s čerstvou relací; co spadne i napodruhé, rozdělí se po senátech. Text se
  bere ze stránky rozhodnutí, pak z PDF (pypdf), a když PDF nemá textovou
  vrstvu, jde modelu PDF celé. Úřední deska ohlašuje vyhlášené rozsudky
  dřív, než je databáze zveřejní. Když pak přijde záznam z databáze se
  stejnou spisovou značkou, převezme od desky první výskyt i shrnutí
  a deska se na webu schová.
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
a změny ukládá vedle jednání. Účastníky, kteří jsou fyzická osoba, drží
archiv jen pod iniciálami; kdo je fyzická osoba, rozhoduje AI, a ptá se jí
po dávkách, ať se odpověď vejde do stropu i s rostoucím archivem. U jména,
kde AI nerozhodne, se celé jméno neuloží; jednání si v takovém případě nechá
účastníky, které mu archiv přiřadil dřív, a úplně nové zůstane jen pod
spisovou značkou, dokud ho některý běh neklasifikuje. Bez toho by jeden
výpadek AI pokaždé shodil jinou část kalendáře zpátky na holé značky.

Senáty a jejich sestavy (předseda, členové, agenda, poznámka ke stážím) jsou
v configu sepsané ručně podle rozvrhu v `rozvrh_zdroj` (`platnost`,
`platnost_od`); `sestavy_navic` jsou senáty jen pro patičku kalendáře, podle
kterých se nefiltruje (správní 15 A a 18 A MSPH). Jednou týdně scraper stáhne
rozvrh ze stránky soudu a AI extrakcí seznam přepíše jen tehdy, když rozvrh
podle titulní strany platí od pozdějšího dne než ten zapsaný – starší ani
stejný dokument ruční seznam nepřepíše. Senáty ze sloupce „Zastupuje senát“
IP senáty jen zastupují a nesledují se. Patička kalendáře „Koho kalendář
sleduje“ je ve výchozím stavu sbalená.

## Přihlášení a vlastní výběr

Přihlášení zajišťuje [Clerk](https://clerk.com) a slouží jen k vlastnímu
výběru. Bez přihlášení (i při výpadku Clerku) web ukazuje výchozí výběr:
u Nejvyššího soudu senát 23 a z ostatních senátů oblasti duševního
vlastnictví a IT, všechny časopisy.

- Web je bez buildu, takže Clerk se načítá skriptem z Frontend API instance
  (`@clerk/clerk-js@6` a komponenty `@clerk/ui@1`), až po vykreslení obsahu.
  Česká lokalizace je v `docs/vendor/clerk-cs-CZ.js`.
- Publishable key je veřejný a je v `docs/app.js` (`CLERK_KLICE`, podle
  hostitele: produkční instance pro `owl.davidzavada.cz`, jinak vývojová).
  Tajný klíč do kódu ani na Vercel nepatří. Bude jen v GitHub secretu
  `CLERK_SECRET_KEY`, až budou přehledy podle výběru.
- Výběr je u účtu v `user.unsafeMetadata.owl`:
  `{"v":1,"ns":{"senaty":[23],"oblasti":[…]},"skryt_procesni":false,"skryte_casopisy":[]}`.
  Nastavuje se na stránce `#nastaveni` (Můj výběr). Neznámé oblasti se
  zahodí, přejmenované převede `alias` v `docs/data/oblasti.json`. Výchozí
  výběr se neukládá.
- Pravidlo: rozhodnutí NS je vidět, když je z vybraného senátu, nebo spadá
  do některé z vybraných oblastí. Pak se případně skryjí rutinní procesní.

## Workflow

- `update-feed.yml` – cron se ozývá každou hodinu, ale scrapuje jen v oknech
  před 7:00 a 14:00 pražského času (GitHub scheduled běhy chodí řídce
  a nepravidelně, proto jsou okna široká). V pondělí ráno navíc `digest.py`.
  Každý scraper je samostatný krok. Když jeden spadne, ostatní doběhnou a
  commit uloží, co se povedlo. Jednání (13–16 minut) jen jednou za 6 hodin.
- `judikatura.yml` – sběr judikatury v nočním okně 23:00–7:00 (hlavní
  dávka, v 7:00 je hotovo) a v denním 9:00–14:00, v okně pokaždé, když od
  posledního běhu uběhlo aspoň 50 minut. Ručně jde pustit jen pro vybrané
  soudy, bez AI nebo s jiným rozpočtem.
- `probe.yml` – jen ručně: stáhne odpovědi webů soudů (formuláře, výpisy,
  detaily, InfoCuria, SPARQL) jako artefakt, s volbou `ulozit` je commitne
  do vybrané větve jako fixtures. Na weby soudů je vidět jen z Actions.
- `tests.yml` – `test_hearings.py` a `test_journals.py` nad uloženými
  originály dokumentů v `tests/fixtures`, `test_judikatura.py` (archiv, fronta,
  AI rozbor, adaptér NS nad simulovaným webem, migrace, kontrola dat v repu)
  a `test_ai.py`.

## Lokálně

```
pip install -r requirements.txt icalendar   # icalendar jen pro testy
python test_hearings.py && python test_journals.py && python test_judikatura.py
python scraper_journals.py                  # a další scrapery stejně
SKIP_GEMINI=1 python scraper_journals.py    # bez AI
SKIP_GEMINI=1 python scraper_judikatura.py --soudy ns   # jen objevování
python scraper_hearings.py --local-jednani MS=tests/fixtures/msph_civilni_2026-08-16_31.docx
```

Stránku stačí otevřít přes libovolný statický server nad `docs/`
(`python -m http.server -d docs`), čte soubory vedle sebe.

## Nasazení

Stránku servíruje Vercel: projekt napojený na tohle repo, bez build kroku,
výstupem je adresář `docs/` (viz `vercel.json`). Nasazuje se jen commit,
který změní `docs/` nebo `vercel.json` (`ignoreCommand`) – commity se
stavem scraperů mimo `docs/` deploy nespouštějí. Doménu (`owl.davidzavada.cz`)
nese záznam CNAME u správce DNS, nasměrovaný na Vercel; GitHub Pages je
vypnuté. Doména webu pro odkaz na kalendář jde přepsat proměnnou
`SITE_HOST`. UID událostí v `hearings.ics` drží doménu `rss.davidzavada.cz`
z doby před přesunem, ať kalendáře nevidí jednání dvakrát.
