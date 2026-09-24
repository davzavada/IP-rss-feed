# Owl – přehled novinek v IP a IT

Statická stránka ([rss.davidzavada.cz](https://rss.davidzavada.cz/)),
kterou dvakrát denně plní scrapery z GitHub Actions. Sleduje rozhodnutí
senátu 23 Cdo Nejvyššího soudu, judikaturu Soudního dvora EU k duševnímu
vlastnictví a IT, články z právních časopisů a nařízená jednání IP senátů
Městského a Vrchního soudu v Praze. Ke všemu dělá AI (Gemini API) heslo
a třívěté shrnutí, jednou týdně z toho napíše dvoutýdenní přehled.

## Jak to drží pohromadě

```
scraper.py           NS 23 Cdo (úřední deska + databáze judikatury)  -> docs/feed.xml
scraper_ipcuria.py   CJEU (ipcuria.eu, InfoCuria, EUR-Lex)           -> docs/ipcuria_feed.xml
scraper_journals.py  časopisy (weby, OJS, Crossref, RSS vydavatelů)  -> docs/journals_feed.xml
scraper_hearings.py  jednání MSPH a VS Praha (.docx/.pdf na justice) -> docs/hearings.json, hearings.ics
digest.py            dvoutýdenní přehled ze tří feedů výše           -> docs/digest.json
feed_common.py       sdílené: první výskyt položek, AI klient, prompty, cache shrnutí
docs/                stránka (index.html, style.css, app.js) a všechno, co čte
tools/probe_zdroje.py sonda: syrové odpovědi webů soudů pro parsery a testy
```

Každý feed si vede **stav prvního výskytu** (`*_seen.json`): kdy položku
poprvé viděl. Podle něj drží položku v okně (NS dva týdny, časopisy čtyři,
CJEU osm) a označuje ji jako novou, když přibyla v posledních 24 hodinách.
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
`GEMINI_API_KEY` scrapery běží dál, jen bez nových shrnutí. Rozhodnutí NS se
shrnuje z přiloženého PDF, a když u něj ve výpisu není, z textu na stránce
rozhodnutí – PDF přikládá soud s odstupem i pár dní, kdežto text tam bývá
hned. Odkaz ve feedu proto vede na stránku rozhodnutí a soubor se nabídne
jako druhý odkaz, jen když opravdu existuje. Když se k textu nedostaneme
vůbec (ani stránka ho nenese, vydavatel ji nepustil), nevymýšlí se nic
a místo shrnutí jde do feedu poznámka; dokud je položka v okně, zkouší se
to každým během znovu. Poznámka mluví jen za nás („shrnutí zatím není"),
ne za zdroj: že rozhodnutí nemáme, neznamená, že ho soud nezveřejnil.

**Kalendář jednání** filtruje přehledy soudů podle `hearings_config.json`:
v civilním úseku na IP senáty (seznam senátů a soudců z rozvrhů práce;
scraper ho umí jednou týdně obnovit AI extrakcí z rozvrhu), v úseku
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

## Workflow

- `update-feed.yml` – cron se ozývá každou hodinu, ale scrapuje jen v oknech
  před 7:00 a 14:00 pražského času (GitHub scheduled běhy chodí řídce
  a nepravidelně, proto jsou okna široká). V pondělí ráno navíc `digest.py`.
- `probe.yml` – jen ručně: stáhne odpovědi webů soudů (formuláře, výpisy,
  detaily, InfoCuria, SPARQL) jako artefakt, s volbou `ulozit` je commitne
  do vybrané větve jako fixtures. Na weby soudů je vidět jen z Actions.
- `tests.yml` – `test_hearings.py` a `test_journals.py` nad uloženými
  originály dokumentů v `tests/fixtures`.

## Lokálně

```
pip install -r requirements.txt icalendar   # icalendar jen pro testy
python test_hearings.py && python test_journals.py
python scraper.py                           # a další scrapery stejně
SKIP_GEMINI=1 python scraper_journals.py    # bez AI
python scraper_hearings.py --local-jednani MS=tests/fixtures/msph_civilni_2026-08-16_31.docx
```

Stránku stačí otevřít přes libovolný statický server nad `docs/`
(`python -m http.server -d docs`), čte soubory vedle sebe.

## Nasazení

Stránku servíruje Vercel: projekt napojený na tohle repo, bez build kroku,
výstupem je adresář `docs/` (viz `vercel.json`). Nasazuje se jen commit,
který změní `docs/` nebo `vercel.json` (`ignoreCommand`) – commity se
stavem scraperů mimo `docs/` deploy nespouštějí. Doménu (`rss.davidzavada.cz`)
nese záznam CNAME u správce DNS; do přepnutí na Vercel stránku dál servíruje
GitHub Pages podle `docs/CNAME`. Doména v UID kalendáře `hearings.ics`
se bere z proměnné `SITE_HOST` (výchozí `rss.davidzavada.cz`), na hostingu
tedy nezávisí.
