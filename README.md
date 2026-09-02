# Owl – přehled novinek v IP a IT

Statická stránka na GitHub Pages ([rss.davidzavada.cz](https://rss.davidzavada.cz/)),
kterou dvakrát denně plní scrapery z GitHub Actions. Sleduje rozhodnutí
senátu 23 Cdo Nejvyššího soudu, judikaturu Soudního dvora EU k duševnímu
vlastnictví a IT, články z právních časopisů a nařízená jednání IP senátů
Městského a Vrchního soudu v Praze. Ke všemu dělá AI (Gemma přes Gemini API)
heslo a třívěté shrnutí, jednou týdně z toho napíše dvoutýdenní přehled.

## Jak to drží pohromadě

```
scraper.py           NS 23 Cdo (úřední deska + databáze judikatury)  -> docs/feed.xml
scraper_ipcuria.py   CJEU (ipcuria.eu, InfoCuria, EUR-Lex)           -> docs/ipcuria_feed.xml
scraper_journals.py  časopisy (weby, OJS, Crossref, RSS vydavatelů)  -> docs/journals_feed.xml
scraper_hearings.py  jednání MSPH a VS Praha (.docx/.pdf na justice) -> docs/hearings.json, hearings.ics
digest.py            dvoutýdenní přehled ze tří feedů výše           -> docs/digest.json
newsletter.py        pošle přehled e-mailem (viz NEWSLETTER.md)
feed_common.py       sdílené: první výskyt položek, AI klient, prompty, cache shrnutí
docs/                stránka (index.html, style.css, app.js) a všechno, co čte
```

Každý feed si vede **stav prvního výskytu** (`*_seen.json`): kdy položku
poprvé viděl. Podle něj drží položku v okně (NS dva týdny, časopisy čtyři,
CJEU osm) a označuje ji jako novou, když přibyla v posledních 24 hodinách.
Tím nezáleží na tom, kdy zdroj položku datuje ani jestli datum později přepíše.

**AI shrnutí** se cachují v `*_meta.json` podle stejného klíče a prořezávají
se spolu se stavem prvního výskytu, takže soubory nerostou donekonečna. Bez
`GEMINI_API_KEY` scrapery běží dál, jen bez nových shrnutí.

**Kalendář jednání** filtruje přehledy soudů na IP senáty podle
`hearings_config.json` (seznam senátů a soudců z rozvrhů práce; scraper ho
umí jednou týdně obnovit AI extrakcí z rozvrhu). Každý nový přehled
porovnává s minulým a změny ukládá vedle jednání.

## Workflow

- `update-feed.yml` – cron se ozývá každou hodinu, ale scrapuje jen v oknech
  před 7:00 a 14:00 pražského času (GitHub scheduled běhy chodí řídce
  a nepravidelně, proto jsou okna široká). V pondělí ráno navíc `digest.py`.
- `newsletter.yml` – pondělí ráno pošle přehled e-mailem; nastavení
  v [NEWSLETTER.md](NEWSLETTER.md).
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
