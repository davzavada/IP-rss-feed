# Newsletter e-mailem

Každé pondělí v 8:30 pražského času odejde dvoutýdenní přehled – ten samý
text, co je na stránce v sekci „Co se stalo v uplynulých dvou týdnech".
Přehled sestaví `digest.py` v pondělí v 6:00, newsletter ho jen přečte
z `docs/digest.json` a rozešle.

- workflow: [`.github/workflows/newsletter.yml`](.github/workflows/newsletter.yml)
- skript: [`newsletter.py`](newsletter.py)
- předloha na hodnoty: [`mail.env.example`](mail.env.example)

Dokud nejsou vyplněné secrets, workflow doběhne a jen napíše, že nemá kam
poslat. Nic se tím nerozbije.

## 1. Heslo aplikace

Poštovní servery dnes přes SMTP nepustí běžné heslo k účtu – potřebuješ
zvláštní heslo pro aplikace. Vytvoř si ho podle svého poskytovatele:

| Poskytovatel | Kde                                                | Server            | Port |
|--------------|----------------------------------------------------|-------------------|------|
| Gmail        | Google účet → Zabezpečení → Hesla pro aplikace     | `smtp.gmail.com`  | 587  |
| Seznam       | Nastavení schránky → Zabezpečení → Hesla aplikací  | `smtp.seznam.cz`  | 465  |
| Fastmail     | Settings → Privacy & Security → App passwords      | `smtp.fastmail.com` | 465 |

U Gmailu je potřeba mít nejdřív zapnuté dvoufázové ověření, jinak se položka
„Hesla pro aplikace" vůbec nenabídne. Heslo se ukáže jen jednou – zkopíruj si
ho rovnou, mezery v něm nevadí.

## 2. Vyplnit secrets

Otevři **Settings → Secrets and variables → Actions → New repository secret**
a založ postupně tyhle položky. Název musí sedět přesně, na velikosti písmen
záleží. Předloha s poznámkami je v `mail.env.example`.

| Název                  | Povinné | Příklad hodnoty              | K čemu                                          |
|------------------------|---------|------------------------------|-------------------------------------------------|
| `MAIL_SMTP_HOST`       | ano     | `smtp.gmail.com`             | adresa SMTP serveru                             |
| `MAIL_SMTP_PORT`       | ne      | `587`                        | 465 = SMTPS, cokoli jiného = STARTTLS; prázdné = 587 |
| `MAIL_USERNAME`        | ano     | `tvoje.adresa@gmail.com`     | přihlášení k SMTP                               |
| `MAIL_PASSWORD`        | ano     | heslo aplikace z kroku 1     | heslo k SMTP                                    |
| `MAIL_FROM`            | ne      | `tvoje.adresa@gmail.com`     | odesílatel; prázdné = `MAIL_USERNAME`           |
| `MAIL_TO`              | ano     | `a@firma.cz, b@firma.cz`     | příjemci, oddělení čárkou nebo novým řádkem     |
| `MAIL_UNSUBSCRIBE_URL` | ne      | `mailto:ty@firma.cz?subject=Odhlaseni` | odkaz na odhlášení v patičce          |
| `MAIL_MAX_AGE_DAYS`    | ne      | `3`                          | jak starý přehled ještě poslat                  |

Hodnotu secretu už si po uložení nepřečteš, jde jen přepsat. Když se v hesle
překlepneš, prostě ho ulož znovu.

### Přidání a odebrání odběratele

Uprav `MAIL_TO` – to je celý seznam. Adresy jdou do **Bcc**, takže je jeden
druhému neuvidí. Nikoho nepřihlašuj bez jeho vědomí; u ručně vedeného seznamu
stačí, že o odběr sám požádal.

## 3. Vyzkoušet

1. **Actions → Týdenní newsletter → Run workflow**, zaškrtni **„Jen sestavit
   náhled, neodesílat"** a spusť. V logu uvidíš, jak starý je přehled a co by
   odešlo; u běhu se objeví artefakt `newsletter` s `newsletter.html`
   a `newsletter.txt`. Náhled si stáhni a otevři v prohlížeči.
2. Když vypadá dobře, spusť to znovu **bez** zaškrtnutí – zpráva se opravdu
   odešle a v logu bude `Odesláno N příjemcům`.
3. Od té chvíle to jede samo každé pondělí.

Ruční spuštění pošle newsletter kdykoli, nezávisle na dni v týdnu.

## Co když nic nepřijde

Podívej se do logu běhu (Actions → Týdenní newsletter → poslední běh):

- **`MAIL_SMTP_HOST není nastavený`** – chybí secrets, vrať se ke kroku 2.
- **`MAIL_TO je prázdné`** – secret existuje, ale nemá hodnotu.
- **`Přehled je starší než 3 dne`** – pondělní běh `update-feed.yml` spadl
  a přehled se nepřegeneroval. Radši se nic neposlalo, aby lidem nepřišel
  týden starý text jako novinka. Spusť ručně `Update RSS Feed` a pak
  newsletter.
- **`Přehled není k dispozici nebo je prázdný`** – `docs/digest.json` chybí
  nebo nemá bloky.
- **`CHYBA odesílání: …`** – server odmítl přihlášení nebo zprávu. U Gmailu
  to skoro vždycky znamená, že heslo není heslo aplikace.
- **Běh se přeskočil úplně** – v logu kroku „Naplánovat běh" je vidět, který
  cron zrovna platí. Pondělní ráno je naplánované dvakrát kvůli letnímu času
  a jeden z těch dvou běhů vždycky hned skončí; to je v pořádku.

Zpráva může skončit ve spamu, když se posílá z Gmailu na hodně adres najednou.
U pár adres to problém není; jakmile by seznam narostl, je lepší přejít na
službu pro newslettery (Buttondown, MailerLite) – měnila by se jen funkce
`send()` v `newsletter.py`.
