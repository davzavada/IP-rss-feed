/* Owl – skript stránky. Čte hotové soubory vedle sebe (data/judikatura/*.json,
   data/casopisy.json, data/oblasti.json, digest.json,
   hearings.json, hearings.ics) a vykresluje je; nic nepočítá, co si už
   spočítaly scrapery. Přihlášení (Clerk) slouží jen k vlastnímu výběru –
   bez něj web ukazuje výchozí výběr. */

/* ========== Pomocné ========== */

/* Hodnoty z feedů escapujeme, aby zvláštní znaky (&, <, >, uvozovky)
   nerozbily stránku a nedal se přes ně vložit cizí HTML kód. */
function esc(s) {
  return String(s).replace(/[&<>"']/g, ch => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
}

// Do href pustíme jen http(s) odkazy (žádné javascript: apod.).
function safeHref(url) {
  return /^https?:\/\//i.test(url) ? esc(url) : "";
}

/* Položky ze všech zdrojů (JSON) převádíme na jednotné objekty, ať buňky
   tabulek nemusí rozlišovat zdroje:
     title, link, doc (PDF), heslo, shrnuti, poznamka, datum, nove, autori
   u judikatury navíc oblasti, senat, druh, procesni, u časopisů casopis a tag. */

// Novinky na úvodní stránce = poprvé viděné za posledních 24 hodin (sběr
// běží jednou denně, takže je to úlovek posledního nočního běhu).
const NOVE_MS = 24 * 60 * 60 * 1000;

function zJson(r) {
  const prvni = Date.parse(r.first_seen || "");
  return {
    title: r.spz || r.nazev || "",
    spz: r.spz || "",
    // Populární název (u ÚS) jde pod značku; bez značky je sám titulkem.
    vec: r.spz && r.nazev ? r.nazev : "",
    link: r.url || "",
    doc: r.pdf && r.pdf !== r.url ? r.pdf : "",
    heslo: r.heslo || "",
    shrnuti: r.shrnuti || "",
    poznamka: r.poznamka || "",
    // Raná předběžná otázka z ipcuria ještě zveřejněná není – datum podání.
    datum: r.zverejneno || r.datum || r.first_seen || "",
    nove: !isNaN(prvni) && Date.now() - prvni < NOVE_MS,
    autori: "",
    oblasti: r.oblasti || [],
    sdeu: /^sdeu:/.test(r.id || ""),
    stav: r.stav_shrnuti || "",
    senat: r.senat,
    druh: r.druh || "",
    procesni: !!r.procesni
  };
}

// Časopisy (data/casopisy.json): štítek je zkratka časopisu z registru.
let CASOPISY = [];                // [{id, zkratka, nazev, vydavatel}]

function zkratkaCasopisu(id) {
  const c = CASOPISY.find(x => x.id === id);
  return c ? c.zkratka : "";
}

function zCasopisu(r) {
  const prvni = Date.parse(r.first_seen || "");
  return {
    title: r.nazev || "",
    link: r.url || "",
    doc: "",
    heslo: r.heslo || "",
    shrnuti: r.shrnuti || "",
    poznamka: r.poznamka || "",
    datum: r.datum || r.first_seen || "",
    nove: !isNaN(prvni) && Date.now() - prvni < NOVE_MS,
    autori: r.autori || "",
    casopis: r.casopis || "",
    tag: zkratkaCasopisu(r.casopis)
  };
}

// „1. 9. 2026" – z ISO data (bez posunu přes UTC, který by ukrojil den)
// i z čehokoli, co přečte Date (pubDate z RSS, časová značka z JSONu).
function czDate(value) {
  const s = String(value || "");
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
  if (m) return +m[3] + ". " + +m[2] + ". " + m[1];
  const d = new Date(s);
  return isNaN(d) ? "" : d.toLocaleDateString("cs-CZ");
}

function fetchJson(url) {
  return fetch(url).then(r => {
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  });
}

function failed(containerId) {
  document.getElementById(containerId).innerHTML = '<p class="feed-empty">Nepodařilo se načíst feed.</p>';
}

/* ========== Štítky ========== */

function tagSlug(label) {
  return String(label).toLowerCase().replace(/[^a-z0-9]+/g, "-");
}

// Štítek časopisu / typu řízení.
function tagBadge(label) {
  if (!label) return "";
  return '<span class="tag tag-' + tagSlug(label) + '">' + esc(label) + "</span>";
}

/* ========== Seznamy položek ========== */

// Vyhledávač NSS posílá PDF ke stažení; na webu na Vercelu ho proto
// odkaz otevře přes náhled (api/pdf.js), který ho prohlížeči předá
// k zobrazení. Jméno souboru na konci adresy je spisová značka – ukáže se
// v titulku karty. Lokálně (bez funkcí Vercelu) vede odkaz rovnou na NSS;
// PDF Nejvyššího soudu prohlížeč ukáže sám.
const NAHLED_PDF = /(^|\.)davidzavada\.cz$|\.vercel\.app$/.test(location.hostname);
const NSS_PDF_RE = /^https:\/\/vyhledavac\.nssoud\.cz\/DokumentOriginal\/Index\/(\d+)$/;

function pdfHref(item) {
  const nss = NAHLED_PDF && NSS_PDF_RE.exec(item.doc || "");
  if (!nss) return safeHref(item.doc);
  const jmeno = item.title.replace(/[/\\]+/g, "-").replace(/[^\p{L}\p{N} ._-]+/gu, "")
    .replace(/\s+/g, " ").trim() || "rozhodnuti";
  return "/pdf/nss/" + nss[1] + "/" + encodeURIComponent(jmeno + ".pdf");
}

// Položka ze zdroje (už převedená přes zJson / zCasopisu) jako řádek
// seznamu. Pořadí čtení: značka · druh · název věci → heslo jako titulek →
// shrnutí → oblasti; datum a odkazy stojí stranou.
function velkym(s) {
  s = String(s || "");
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : "";
}

// Popisek odkazu na zdroj podle toho, kam vede.
function zdrojLabel(url) {
  if (/^https?:\/\/eur-lex\.europa\.eu\//i.test(url)) return "EUR-Lex";
  if (/^https?:\/\/curia\.europa\.eu\//i.test(url)) return "Curia";
  return "Zdroj";
}

// Doplní k položce, co potřebuje řádek: značku, druh, titulek, název věci
// pod ním a štítky. U judikatury je titulkem heslo (značka jde nad něj),
// u článků název článku a heslo je štítek.
function pripravPolozku(item, key) {
  item._src = key;
  if (key === "journals") {
    item.ident = item.tag || "";
    item.druhText = "Článek";
    item.titul = item.title.replace(/^\[[^\]]+\]\s*/, "");
    item.vecText = item.autori || "";
    item.stitky = item.heslo ? [item.heslo] : [];
  } else {
    item.ident = item.spz || "";
    item.druhText = velkym(item.druh);
    item.titul = item.heslo || item.vec || item.title;
    item.vecText = item.vec && item.vec !== item.titul ? item.vec : "";
    if (item.titul === item.ident) item.ident = "";
    item.stitky = bezDuplicit((item.oblasti || []).map(o => OBLASTI[ALIAS_OBLASTI[o] || o]).filter(Boolean));
  }
  return item;
}

const TECKA = '<span aria-hidden="true">·</span>';

function polozkaHtml(p) {
  const meta = [];
  if (p.ident) meta.push('<span class="polozka-ident">' + esc(p.ident) + "</span>");
  if (p.druhText) meta.push("<span>" + esc(p.druhText) + "</span>");
  let html = '<article class="polozka"><div class="polozka-meta">' + meta.join(TECKA);
  if (p.vecText) {
    html += '<span class="meta-vec">' + (meta.length ? TECKA : "") + "<span>" + esc(p.vecText) + "</span></span>";
  }
  html += "</div>";
  const datum = czDate(p.datum);
  html += '<span class="polozka-datum">' + esc(datum) + "</span>";
  html += '<h3 class="polozka-titul">' + esc(p.titul) + "</h3>";
  if (p.vecText) html += '<div class="polozka-vec">' + esc(p.vecText) + "</div>";
  // Bez shrnutí ještě může být poznámka, proč žádné není – třeba že u žádosti
  // o předběžnou otázku zatím nejsou zveřejněné otázky.
  if (p.shrnuti) html += '<p class="polozka-shrnuti">' + esc(p.shrnuti) + "</p>";
  else if (p.poznamka) html += '<p class="polozka-shrnuti note">' + esc(p.poznamka) + "</p>";
  html += '<div class="polozka-stitky">' + p.stitky.map(s => '<span class="stitek">' + esc(s) + "</span>").join("");
  if (p.stitky.length > 1) {
    html += '<button type="button" class="stitek stitek-vic" title="' + esc(p.stitky.join(" · ")) +
      '" aria-label="Ukázat všechny oblasti: ' + esc(p.stitky.join(", ")) + '">…</button>';
  }
  html += "</div>";
  // Odkaz ještě na samotný dokument (PDF rozhodnutí) – shrnutí je jen
  // shrnutí. Obojí se otevře v nové kartě, ať čtenář nepřijde o místo.
  const doc = pdfHref(p);
  const zdroj = safeHref(p.link);
  html += '<div class="polozka-odkazy">';
  if (doc) html += '<a class="odkaz odkaz-pdf" href="' + doc + '" target="_blank" rel="noopener">PDF</a>';
  if (zdroj) html += '<a class="odkaz" href="' + zdroj + '" target="_blank" rel="noopener">' + zdrojLabel(p.link) + " ↗</a>";
  html += "</div></article>";
  return html;
}

/* ========== Oblasti a výběr (výchozí, nebo uložený u účtu) ========== */
// Seznam oblastí a senátů NS čte stránka z data/ (stejné soubory jako AI).
// Nepřihlášený vidí výchozí výběr (IP a IT); přihlášený svůj, uložený
// v Clerku jako user.unsafeMetadata.owl:
//   {"v":1,"ns":{"oblasti":[…],"senaty":[23]},"nss":{"oblasti":[…]},
//    "us":{…},"sdeu":{…},"skryte_casopisy":[]}
// (Dřívější „skryt_procesni" se ignoruje – procesní rozhodnutí jsou vidět vždy.)
let OBLASTI = {};                 // id -> název
let OBLASTI_SEZNAM = [];          // oblasti v pořadí seznamu (se skupinou)
let ALIAS_OBLASTI = {};           // přejmenované id -> nové
let VYCHOZI_OBLASTI = [];
let VYCHOZI_SENATY_NS = [23];
let SENATY_NS = [];               // [{senat, kolegium, popis}]

// Soudy, u kterých se filtruje podle oblastí. Výběr je uložený u každého
// soudu zvlášť, ale stránka Můj výběr ho zatím nastavuje všem stejně.
// `nazev2` je 2. pád do vět.
const SOUDY_VYBERU = [
  { soud: "ns", zkratka: "NS", nazev: "Nejvyšší soud", nazev2: "Nejvyššího soudu" },
  { soud: "nss", zkratka: "NSS", nazev: "Nejvyšší správní soud", nazev2: "Nejvyššího správního soudu" },
  { soud: "us", zkratka: "ÚS", nazev: "Ústavní soud", nazev2: "Ústavního soudu" },
  { soud: "sdeu", zkratka: "SDEU", nazev: "Soudní dvůr EU", nazev2: "Soudního dvora EU" }
];

let vyber = null;                 // platný výběr (výchozí nebo z účtu)

function nastavVyber(oblasti, senaty) {
  if (oblasti && Array.isArray(oblasti.oblasti)) {
    OBLASTI = {};
    OBLASTI_SEZNAM = oblasti.oblasti;
    oblasti.oblasti.forEach(o => { OBLASTI[o.id] = o.nazev; });
    ALIAS_OBLASTI = oblasti.alias || {};
    VYCHOZI_OBLASTI = oblasti.oblasti.filter(o => o.vychozi).map(o => o.id);
  }
  if (senaty) {
    if (Array.isArray(senaty.vychozi)) VYCHOZI_SENATY_NS = senaty.vychozi;
    if (Array.isArray(senaty.senaty)) SENATY_NS = senaty.senaty;
  }
  vyber = vychoziVyber();
}

function vychoziVyber() {
  const v = { v: 1, skryte_casopisy: [] };
  SOUDY_VYBERU.forEach(s => { v[s.soud] = { oblasti: VYCHOZI_OBLASTI.slice() }; });
  v.ns.senaty = VYCHOZI_SENATY_NS.slice();
  return v;
}

function bezDuplicit(seznam) {
  return seznam.filter((x, i) => seznam.indexOf(x) === i);
}

// Uložený výběr projde přes známá id (přejmenované oblasti přes alias);
// co v něm chybí, doplní výchozí – účet tak přežije i nové soudy a oblasti.
function normalizujVyber(ulozeny) {
  const v = vychoziVyber();
  if (!ulozeny || typeof ulozeny !== "object") return v;
  SOUDY_VYBERU.forEach(s => {
    const u = ulozeny[s.soud];
    if (!u || !Array.isArray(u.oblasti)) return;
    v[s.soud].oblasti = bezDuplicit(u.oblasti.map(o => ALIAS_OBLASTI[o] || o).filter(o => OBLASTI[o]));
  });
  if (ulozeny.ns && Array.isArray(ulozeny.ns.senaty)) {
    v.ns.senaty = bezDuplicit(ulozeny.ns.senaty.map(Number).filter(n => Number.isInteger(n) && n > 0));
  }
  if (Array.isArray(ulozeny.skryte_casopisy)) {
    // Starší výběry ukládaly zkratku („GRUR Int"), teď se ukládá id z registru.
    const idCasopisu = c => (CASOPISY.find(x => x.zkratka === c) || {}).id || c;
    v.skryte_casopisy = bezDuplicit(ulozeny.skryte_casopisy.filter(c => typeof c === "string")
      .map(idCasopisu));
  }
  return seradVyber(v);
}

function jeVychozi(v) {
  return JSON.stringify(v) === JSON.stringify(vychoziVyber());
}

// Rozhodnutí NS je vidět, když je z vybraného senátu, nebo když spadá do
// některé z vybraných oblastí.
function vidiNS(item) {
  const v = vyber || vychoziVyber();
  if (v.ns.senaty.indexOf(item.senat) >= 0) return true;
  return (item.oblasti || []).some(o => v.ns.oblasti.indexOf(o) >= 0);
}

// NSS a ÚS: rozhodnutí je vidět, když spadá do některé z oblastí vybraných
// u jeho soudu (dokud ho AI nezařadí, platí oblasti podle úředních údajů).
function vidiPodleOblasti(soud) {
  return item => {
    const v = vyber || vychoziVyber();
    return (item.oblasti || []).some(o => v[soud].oblasti.indexOf(o) >= 0);
  };
}

function vidiCasopis(item) {
  const v = vyber || vychoziVyber();
  return v.skryte_casopisy.indexOf(item.casopis) < 0;
}

// Zdroje stránky: okna judikatury (data/judikatura/) a časopisy
// (data/casopisy.json); `filtr` je výběr, co z okna ukázat. `nazev`
// a `ikona` jsou do nadpisu skupiny v Novinkách.
const PRAZDNY_VYBER = 'Ve vašem výběru za tu dobu nic nepřibylo. <a href="#nastaveni">Upravit výběr</a>';
const FEEDS = [
  { key: "nsoud",    label: "NS",      nazev: "Nejvyšší soud", ikona: "icon-court",
    json: "data/judikatura/ns.json",
    containerId: "feed-nsoud", filtr: vidiNS, prazdno: PRAZDNY_VYBER, stavId: "stav-nsoud" },
  { key: "nss",      label: "NSS",     nazev: "Nejvyšší správní soud", ikona: "icon-scale",
    json: "data/judikatura/nss.json",
    containerId: "feed-nss", filtr: vidiPodleOblasti("nss"), prazdno: PRAZDNY_VYBER, stavId: "stav-nss" },
  { key: "us",       label: "ÚS",      nazev: "Ústavní soud", ikona: "icon-shield",
    json: "data/judikatura/us.json",
    containerId: "feed-us", filtr: vidiPodleOblasti("us"), prazdno: PRAZDNY_VYBER, stavId: "stav-us" },
  { key: "sdeu",     label: "SDEU",    nazev: "Soudní dvůr EU", ikona: "icon-eu",
    json: "data/judikatura/sdeu.json",
    containerId: "feed-sdeu", filtr: vidiPodleOblasti("sdeu"), prazdno: PRAZDNY_VYBER, stavId: "stav-sdeu" },
  { key: "journals", label: "Časopis", nazev: "Právní časopisy", ikona: "icon-book",
    json: "data/casopisy.json", prevod: zCasopisu,
    containerId: "feed-journals", filtr: vidiCasopis, prazdno: PRAZDNY_VYBER }
];

// Stránka zdroje v navigaci (časopisy mají stránku #casopisy).
const STRANKA_ZDROJE = { journals: "casopisy" };

// V boční navigaci u každého zdroje nenápadně vpravo, za jak dlouhou dobu
// ukazuje novinky (okno dat, okno_dni v JSON – u soudů 14 nebo 30 dní).
function ukazOkna() {
  FEEDS.forEach(f => {
    const a = document.querySelector('#sidenav a[href="#' + (STRANKA_ZDROJE[f.key] || f.key) + '"]');
    if (!a || !f.oknoDni) return;
    let el = a.querySelector(".nav-okno");
    if (!el) {
      el = document.createElement("span");
      el.className = "nav-okno";
      a.appendChild(el);
    }
    // Celé týdny od tří výš (okno časopisů) jako týdny, jinak ve dnech.
    el.textContent = f.oknoDni >= 21 && f.oknoDni % 7 === 0
      ? tvar(f.oknoDni / 7, "týden", "týdny", "týdnů") : tvar(f.oknoDni, "den", "dny", "dní");
    el.title = "Novinky za posledních " + el.textContent;
  });
}

// Položky zdroje jako objekty; u zdroje si poznamená, kdy byl aktualizován.
function nactiZdroj(f) {
  return fetchJson(f.json).then(d => {
    f.aktualizovano = d.generated || "";
    f.oknoDni = d.okno_dni || 0;
    if (Array.isArray(d.casopisy)) nastavCasopisy(d.casopisy);
    return (d.polozky || []).map(r => pripravPolozku((f.prevod || zJson)(r), f.key));
  });
}

// `prazdno` je HTML hlášky, když nic není (u filtrovaných stránek odkaz na výběr).
function renderSeznam(items, container, prazdno) {
  if (items.length === 0) {
    container.innerHTML = '<p class="feed-empty">' + (prazdno || "Žádné nové položky.") + "</p>";
    return;
  }
  container.innerHTML = '<div class="zdroj">' + items.map(polozkaHtml).join("") + "</div>";
}

/* ========== Novinky: posledních 24 hodin ========== */
// Seskupené podle zdroje (v pořadí FEEDS), nad nimi přepínač Vše /
// Judikatura / Časopisy s počty.
const FILTRY_NOVINEK = [
  { k: "vse", label: "Vše", bere: () => true },
  { k: "jud", label: "Judikatura", bere: i => i._src !== "journals" },
  { k: "cas", label: "Časopisy", bere: i => i._src === "journals" }
];
let novinkyFiltr = "vse";
let novinkyVysledky = null;       // poslední profiltrované výsledky zdrojů

// `results` jsou výsledky Promise.allSettled nad položkami jednotlivých feedů.
function renderToday(results) {
  novinkyVysledky = results;
  const container = document.getElementById("feed-today");
  const prepinac = document.getElementById("novinky-filtr");
  const pocet = document.getElementById("nav-pocet");
  // Když selžou všechny feedy, není to „nic nového", ale chyba načítání.
  if (results.every(r => r.status === "rejected")) {
    failed("feed-today");
    return;
  }
  const polozky = [];
  results.forEach(r => {
    if (r.status === "fulfilled") r.value.forEach(item => { if (item.nove) polozky.push(item); });
  });
  if (pocet) pocet.textContent = polozky.length || "";
  if (prepinac) {
    prepinac.hidden = polozky.length === 0;
    prepinac.innerHTML = FILTRY_NOVINEK.map(f =>
      '<button type="button" data-filtr="' + f.k + '" aria-pressed="' + (f.k === novinkyFiltr) + '"><span>' +
      f.label + '</span><span class="segment-pocet">' + polozky.filter(f.bere).length + "</span></button>").join("");
  }
  if (polozky.length === 0) {
    container.innerHTML = '<p class="feed-empty">Za posledních 24 hodin nic nepřibylo. ' +
      "Sběr běží jednou denně ve 2:00 v noci.</p>";
    return;
  }
  const filtr = FILTRY_NOVINEK.find(f => f.k === novinkyFiltr) || FILTRY_NOVINEK[0];
  const html = FEEDS.map(f => {
    const skupina = polozky.filter(i => i._src === f.key && filtr.bere(i))
      .sort((a, b) => new Date(b.datum) - new Date(a.datum));
    if (!skupina.length) return "";
    return '<section class="zdroj" aria-label="' + esc(f.nazev) + '"><div class="zdroj-hlava">' +
      '<svg class="nav-ico ico-' + f.key + '" aria-hidden="true"><use href="#' + f.ikona + '"></use></svg>' +
      '<span class="zdroj-nazev">' + esc(f.nazev) + '</span><span class="zdroj-pocet">' + skupina.length +
      "</span></div>" + skupina.map(polozkaHtml).join("") + "</section>";
  }).join("");
  container.innerHTML = html || '<p class="feed-empty">V téhle části za posledních 24 hodin nic nepřibylo.</p>';
}

function initNovinkyFiltr() {
  const prepinac = document.getElementById("novinky-filtr");
  if (prepinac) prepinac.addEventListener("click", e => {
    const b = e.target.closest("button[data-filtr]");
    if (!b || !novinkyVysledky) return;
    novinkyFiltr = b.dataset.filtr;
    renderToday(novinkyVysledky);
    pripojVychozi(document.getElementById("feed-today"), "today");
    const znovu = prepinac.querySelector('[data-filtr="' + novinkyFiltr + '"]');
    if (znovu) znovu.focus();
  });
  // Na telefonu je u položky vidět jen první oblast a „…" – klepnutí
  // ukáže všechny.
  document.addEventListener("click", e => {
    const vic = e.target.closest(".stitek-vic");
    if (vic) vic.parentElement.classList.add("vse");
  });
}

// Stažené položky zdrojů (výsledky Promise.allSettled). Při změně výběru se
// jen znovu profiltrují a vykreslí – nic se znovu nestahuje.
let zdrojeVysledky = null;

// Nad kartou judikatury: kolik rozhodnutí z celého okna (ne jen z výběru)
// ještě nemá shrnutí. Rozhodnutí bez oblasti se do výběru podle oblastí
// dostanou až po AI, tak ať je jasné, že ještě přibudou. (U NSS a ÚS má
// většina oblast už podle údajů soudu.)
function stavShrnutiText(polozky, oknoDni) {
  const cekajici = polozky.filter(i => i.stav === "pripravuje");
  const bezOblasti = cekajici.filter(i => !(i.oblasti || []).length).length;
  const bezTextu = polozky.filter(i => i.stav === "ceka_na_text").length;
  const casti = [];
  if (cekajici.length) {
    casti.push("AI ještě zpracovává " + cekajici.length + " z " + polozky.length + " rozhodnutí" +
      (oknoDni ? " za posledních " + oknoDni + " dní" : "") + ". Shrnutí a oblasti doplní " +
      "při nočním sběru (ve 2:00)" +
      (bezOblasti === cekajici.length
        ? ", do výběru podle oblastí se tato rozhodnutí dostanou až potom."
        : bezOblasti
          ? "; " + bezOblasti + " z nich zatím " +
            (bezOblasti >= 2 && bezOblasti <= 4
              ? "nemají oblast a do výběru podle oblastí se dostanou až potom."
              : "nemá oblast a do výběru podle oblastí se dostane až potom.")
          : ", do té doby jsou zařazená podle údajů soudu."));
  }
  if (bezTextu) casti.push("U " + bezTextu + " rozhodnutí soud ještě nezveřejnil text.");
  return casti.join(" ");
}

function vykresliStavShrnuti() {
  FEEDS.forEach((f, idx) => {
    const el = f.stavId && document.getElementById(f.stavId);
    if (!el) return;
    const r = zdrojeVysledky[idx];
    const text = r.status === "fulfilled" ? stavShrnutiText(r.value, f.oknoDni) : "";
    el.textContent = text;
    el.hidden = !text;
  });
}

function vykresliZdroje() {
  if (!zdrojeVysledky) return;
  vykresliStavShrnuti();
  const filtrovane = zdrojeVysledky.map((r, idx) => {
    const f = FEEDS[idx];
    if (r.status !== "fulfilled" || !f.filtr) return r;
    return { status: "fulfilled", value: r.value.filter(f.filtr) };
  });
  FEEDS.forEach((f, idx) => {
    if (filtrovane[idx].status === "rejected") {
      failed(f.containerId);
      return;
    }
    // Druhá stránka je úplný výpis za okno feedu – nové položky z ní
    // nevynecháváme, na jednu stránku se položky nedostanou dvakrát.
    const el = document.getElementById(f.containerId);
    renderSeznam(filtrovane[idx].value, el, f.prazdno);
    pripojVychozi(el, f.key);
  });
  renderToday(filtrovane);
  pripojVychozi(document.getElementById("feed-today"), "today");
}

// Nepřihlášený vidí výchozí výběr – pod tabulkou judikatury mu to řekneme
// a nabídneme přihlášení. Bez Clerku (výpadek) nic, přihlásit se nejde.
const KARTY_S_VYBEREM = ["nsoud", "nss", "us", "sdeu", "today"];

function pripojVychozi(el, key) {
  if (!el || clerkStav !== "pripraven" || prihlaseny || KARTY_S_VYBEREM.indexOf(key) < 0) return;
  el.insertAdjacentHTML("beforeend", '<p class="vychozi-pozn">Ve výchozím nastavení se ukazují jen ' +
    "rozhodnutí z oblasti IP a IT" + (key === "nsoud" ? " a všechna rozhodnutí senátu 23 Cdo" : "") +
    '. Chcete-li vlastní výběr, <a href="#nastaveni">přihlaste se</a> a nastavte si ho.</p>');
}

/* ========== Dvoutýdenní přehled (digest.json) ========== */

// Klíč zdroje pouštíme do class jen z uzavřeného seznamu – ať se přes data
// z JSONu nedá do stránky propašovat cizí třída.
const SRC_KEYS = FEEDS.map(f => f.key);

function digestSource(s) {
  const key = SRC_KEYS.indexOf(s.src) >= 0 ? s.src : "";
  const badge = '<span class="src' + (key ? " src-" + key : "") + '">' + esc(s.label || "") + "</span>";
  // U článků ještě zkratka časopisu ([JIPLP], [IIC], …), u CJEU typ řízení.
  const inner = badge + tagBadge(s.tag) + "<span>" + esc(s.title || "") + "</span>";
  const href = safeHref(s.link || "");
  return href
    ? '<a class="digest-source" href="' + href + '">' + inner + "</a>"
    : '<span class="digest-source">' + inner + "</span>";
}

function renderDigest(data) {
  const container = document.getElementById("feed-digest");
  // Přehled se na rozdíl od feedů generuje jen jednou týdně, takže datum
  // poslední aktualizace patří k němu – to v hlavičce stránky je z feedů.
  const stamp = document.getElementById("digest-updated");
  if (stamp && data && data.generated) {
    stamp.textContent = "Aktualizováno " + czDate(data.generated) + ".";
  }
  if (!data || !Array.isArray(data.blocks) || data.blocks.length === 0) {
    container.innerHTML = '<p class="feed-empty">Přehled zatím není k dispozici.</p>';
    return;
  }
  let html = "";
  if (data.intro) html += '<p class="digest-intro">' + esc(data.intro) + "</p>";
  html += '<div class="digest-bloky">';
  data.blocks.forEach(b => {
    html += '<div class="digest-block">';
    html += '<h3 class="digest-title">' + esc(b.title || "") + "</h3>";
    html += '<p class="digest-text">' + esc(b.text || "") + "</p>";
    const sources = Array.isArray(b.sources) ? b.sources : [];
    if (sources.length) {
      html += '<div class="digest-sources">' + sources.map(digestSource).join("") + "</div>";
    }
    html += "</div>";
  });
  html += "</div>";

  const from = czDate(data.from), to = czDate(data.to);
  const meta = [];
  if (data.total) meta.push("vybráno z " + data.total + " položek");
  if (from && to) meta.push("období " + from + " – " + to);
  if (meta.length) {
    html += '<p class="digest-meta">' + esc(meta.join(" · ")) + "</p>";
  }
  container.innerHTML = html;
}

/* ========== Kalendář jednání (hearings.json) ========== */
// Jednání MS a VS Praha v agendě duševního vlastnictví. Dva pohledy:
// „3 týdny" (mřížka ve stylu Google Kalendáře, detail jako bublina vedle
// štítku) a „Seznam" nadcházejících jednání po dnech. Na telefonu je
// vidět jen seznam.
const CAL_DOWS = ["Po", "Út", "St", "Čt", "Pá"];
const DNY = ["Neděle", "Pondělí", "Úterý", "Středa", "Čtvrtek", "Pátek", "Sobota"];
const MESICE = ["ledna", "února", "března", "dubna", "května", "června", "července",
                "srpna", "září", "října", "listopadu", "prosince"];
// Kalendář neukazuje měsíc, ale okno tří týdnů, které začíná tímhle
// týdnem: minulé týdny už si nikdo nevypisuje a soudy stejně vypisují
// jednání jen zhruba na dva týdny dopředu. Šipky posouvají o týden,
// takže do minulosti se dá dojít, když je potřeba.
const CAL_TYDNU = 3;
const CAL_POSUN_DNU = 7;

let calData = null;      // obsah hearings.json
let calStart = null;     // pondělí prvního zobrazeného týdne (Date)
let calPohled = "mesic"; // mesic | seznam
let calSoudy = { MS: true, VS: true };
let calPopKey = null;    // co je rozkliknuté: "datum#idx"

// ISO datum z lokálního Date – toISOString by přes UTC ukrajovalo den.
function isoOf(d) {
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") +
    "-" + String(d.getDate()).padStart(2, "0");
}

// Pondělí týdne, do kterého datum spadá.
function mondayOf(d) {
  const p = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  p.setDate(p.getDate() - ((p.getDay() + 6) % 7));
  return p;
}


// Výchozí okno: začíná pondělím tohoto týdne.
function calDefaultStart() {
  return mondayOf(new Date());
}

// „21. 9. – 9. 10. 2026"; rok u začátku jen tehdy, když se okno láme přes něj.
function calRangeLabel(od, do_) {
  const den = d => d.getDate() + ". " + (d.getMonth() + 1) + ".";
  const zacatek = den(od) + (od.getFullYear() === do_.getFullYear()
    ? "" : " " + od.getFullYear());
  return zacatek + " – " + den(do_) + " " + do_.getFullYear();
}

// Datum z ISO řetězce v místním čase (new Date("2026-09-29") by bralo UTC).
function dateOf(iso) {
  const [y, m, d] = String(iso).split("-").map(Number);
  return new Date(y, m - 1, d);
}

// „Úterý 29. září"
function denNazev(iso) {
  const d = dateOf(iso);
  return DNY[d.getDay()] + " " + d.getDate() + ". " + MESICE[d.getMonth()];
}

// „dnes", „zítra", „za 4 dny"
function zaKolik(iso) {
  const dny = Math.round((dateOf(iso) - dateOf(isoOf(new Date()))) / 864e5);
  if (dny === 0) return "dnes";
  if (dny === 1) return "zítra";
  return "za " + tvar(dny, "den", "dny", "dní");
}

// „9:30" vs „10:00" se řetězcově řadí špatně (dokumenty píšou i jednocifernou
// hodinu), proto porovnáváme minuty od půlnoci; jednání bez času jdou na konec.
function minutesOf(hodina) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(hodina || ""));
  return m ? Number(m[1]) * 60 + Number(m[2]) : 24 * 60 + 1;
}


// Jednání po aplikaci filtrů, seskupená podle ISO data.
function calEventsByDay() {
  const byDay = {};
  (calData.jednani || []).forEach(j => {
    if (!j || !j.datum || !calSoudy[j.soud] || !j.ip) return;
    (byDay[j.datum] = byDay[j.datum] || []).push(j);
  });
  Object.values(byDay).forEach(list => list.sort(
    (a, b) => minutesOf(a.hodina) - minutesOf(b.hodina) ||
      String(a.spz || "").localeCompare(String(b.spz || ""), "cs")));
  return byDay;
}

// Jméno sporu („OSA v. BH Drink") počítá scraper; starší data ho nemají,
// tak padáme zpátky na spisovou značku.
function caseName(j) {
  return j.nazev || j.spz || "Jednání";
}

function infosoudUrl(j) {
  const court = (calData.courts || {})[j.soud] || {};
  // Rejstřík musí jít malými písmeny (druhVeci=co), jinak InfoSoud řízení
  // nenajde.
  return "https://infosoud.gov.cz/InfoSoud/detail-rizeni?typOrganizace=VSECHNY_KRAJE" +
    "&druhOrganizace=" + encodeURIComponent(court.infosoud_org || "") +
    "&cisloSenatu=" + encodeURIComponent(j.cislo_senatu) +
    "&druhVeci=" + encodeURIComponent(String(j.rejstrik || "").toLowerCase()) +
    "&bcVec=" + encodeURIComponent(j.bc) +
    "&rocnik=" + encodeURIComponent(j.rocnik);
}


function courtName(j) {
  const court = (calData.courts || {})[j.soud] || {};
  return court.nazev || j.soud;
}

// Štítek v mřížce: barevná tečka soudu, čas a jméno sporu bez „a další"
// (celé je v title a v bublině).
function chipHtml(j, iso, idx) {
  const chip = j.soud === "MS" ? "cal-chip-ms" : "cal-chip-vs";
  return '<button type="button" class="cal-chip ' + chip +
    '" data-date="' + iso + '" data-idx="' + idx + '" aria-expanded="false" title="' +
    esc(caseName(j) + (j.spz ? " · " + j.spz : "")) + '">' +
    '<span class="cal-dot"></span>' +
    (j.hodina ? '<span class="cal-chip-time">' + esc(j.hodina) + "</span>" : "") +
    '<span class="cal-chip-name">' + esc(caseName(j).replace(/ a další$/, "")) + "</span></button>";
}

function renderCalGrid() {
  const byDay = calEventsByDay();
  const start = new Date(calStart);
  const todayIso = isoOf(new Date());

  // Víkendy soudy nezasedají – mřížka má jen pracovní dny.
  let html = CAL_DOWS.map(d => '<div class="cal-dow">' + d + "</div>").join("");
  for (let w = 0; w < CAL_TYDNU; w++) {
    for (let i = 0; i < 5; i++) {
      const d = new Date(start);
      d.setDate(start.getDate() + w * 7 + i);
      const iso = isoOf(d);
      const cls = ["cal-day"];
      if (iso === todayIso) cls.push("today");
      else if (iso < todayIso) cls.push("past");
      // U prvního dne v měsíci patří k číslu i měsíc – jinak by „1" nešlo zařadit.
      const cislo = d.getDate() === 1 ? "1. " + (d.getMonth() + 1) + "." : d.getDate();
      html += '<div class="' + cls.join(" ") + '" data-sloupec="' + i + '" data-tyden="' + w + '">' +
        '<div class="cal-daynum"><span' + (iso === todayIso ? ' aria-label="dnes ' + cislo + '"' : "") + ">" +
        cislo + "</span></div>" +
        (byDay[iso] || []).map((j, idx) => chipHtml(j, iso, idx)).join("") + "</div>";
    }
  }
  const konec = new Date(start);
  konec.setDate(start.getDate() + (CAL_TYDNU - 1) * 7 + 4);
  document.getElementById("cal-grid").innerHTML = html;
  document.getElementById("cal-title").textContent = calRangeLabel(start, konec);
  document.getElementById("cal-today").hidden = isoOf(calStart) === isoOf(calDefaultStart());
}

// Dokdy soudy zveřejnily přehled: konec nejpozdějšího období ze
// staženích přehledů, bez něj datum posledního jednání.
function zverejnenoDo() {
  let konec = "";
  Object.values(calData.courts || {}).forEach(c => {
    Object.values(c.useky || {}).forEach(u => {
      if (u && u.obdobi && u.obdobi.do > konec) konec = u.obdobi.do;
    });
  });
  if (!konec) (calData.jednani || []).forEach(j => { if (j.datum > konec) konec = j.datum; });
  return konec;
}

// Seznam jen nadcházejících jednání (proběhlá jsou v pohledu 3 týdny).
function renderCalSeznam() {
  const byDay = calEventsByDay();
  const todayIso = isoOf(new Date());
  const dny = Object.keys(byDay).sort().filter(d => d >= todayIso);
  let html = dny.map(iso => '<div class="cal-den"><div class="cal-den-hlava">' +
    '<span class="cal-den-nazev">' + esc(denNazev(iso)) + "</span>" +
    '<span class="cal-den-rel">' + esc(zaKolik(iso) + " · " + byDay[iso].length + " jednání") + "</span></div>" +
    byDay[iso].map((j, idx) => {
      const soud = j.soud === "MS" ? "ms" : "vs";
      const url = esc(infosoudUrl(j));
      const meta = ['<span class="cal-soud cal-soud-' + soud + '" title="' + esc(courtName(j)) + '">' + esc(j.soud) + "</span>"];
      if (j.spz) meta.push('<span class="cal-spz">' + esc(j.spz) + "</span>");
      if (j.predseda) meta.push('<span class="cal-predseda" aria-hidden="true">·</span><span class="cal-predseda">' + esc(j.predseda) + "</span>");
      if (j.sin) meta.push('<span aria-hidden="true">·</span><span>síň ' + esc(j.sin) + "</span>");
      return '<div class="cal-radek"><span class="cal-radek-cas">' + esc(j.hodina || "–") + "</span>" +
        '<div class="cal-radek-telo"><div class="cal-radek-nazev">' + esc(caseName(j)) + "</div>" +
        '<div class="cal-radek-meta">' + meta.join("") + "</div></div>" +
        '<div class="cal-radek-akce"><a class="odkaz" href="' + url + '" target="_blank" rel="noopener">InfoSoud ↗</a>' +
        (j.uid ? '<button type="button" class="odkaz cal-ics" data-date="' + iso + '" data-idx="' + idx +
          '" title="Uložit tohle jednání jako událost do vlastního kalendáře">.ics</button>' : "") + "</div>" +
        '<a class="cal-radek-ikona" href="' + url + '" target="_blank" rel="noopener" title="Otevřít v InfoSoudu" ' +
        'aria-label="Otevřít v InfoSoudu"><svg class="nav-ico" aria-hidden="true"><use href="#icon-court"></use></svg></a></div>';
    }).join("") + "</div>").join("");
  if (!dny.length) html = '<p class="feed-empty">Žádná nadcházející jednání soudy zatím nezveřejnily.</p>';
  const doKdy = zverejnenoDo();
  if (doKdy) {
    const d = dateOf(doKdy);
    html += '<div class="cal-pozn">Soudy zatím zveřejnily jednání do ' + d.getDate() + ". " + (d.getMonth() + 1) +
      ". Přehled na další dny přibude, až ho vydají.</div>";
  }
  document.getElementById("cal-seznam").innerHTML = html;
}

// Počty nadcházejících jednání u štítků soudů (bez ohledu na filtr).
function renderCalPocty() {
  const todayIso = isoOf(new Date());
  ["MS", "VS"].forEach(s => {
    const el = document.getElementById("cal-pocet-" + s);
    if (el) el.textContent = (calData.jednani || []).filter(j => j && j.ip && j.soud === s && j.datum >= todayIso).length;
  });
}

// Co se s jednáním stalo od minulého přehledu. Scraper porovnává každý
// nový přehled s tím, co už má, a změny i s popisem ukládá do hearings.json.
function zmenaText(z) {
  const popis = z.popis || z.typ;
  if (z.typ === "presun") return popis + " z " + czDate(z.z);
  if (z.typ === "cas" || z.typ === "sin") return popis + " (dřív " + z.z + ")";
  return popis;
}

function zmenyJednani(j) {
  return (calData.zmeny || []).filter(z =>
    z.soud === j.soud && z.spz === j.spz && z.datum === j.datum);
}

/* ========== Jedno jednání jako .ics ========== */
// Celý kalendář se dá přihlásit z hearings.ics; tohle je pro jedno jednání,
// které si člověk chce prostě jen jednou hodit do svého kalendáře. Událost
// se vyřízne z hotového hearings.ics podle uid (scraper ho ukládá i do
// hearings.json), takže stažená událost je do písmene ta, kterou má
// přihlášený kalendář – a nic se tu neskládá podruhé.
let icsPromise = null;

function fetchIcs() {
  if (!icsPromise) {
    icsPromise = fetch("hearings.ics").then(r => {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.text();
    });
  }
  return icsPromise;
}

// Vlastnosti celého kalendáře (jméno, popis, interval obnovy) do jedné
// události nepatří – klient by si podle nich založil nový kalendář.
const ICS_DROP_RE = /^(X-WR-CALNAME|X-WR-CALDESC|REFRESH-INTERVAL|X-PUBLISHED-TTL)/;

function icsForEvent(j) {
  return fetchIcs().then(ics => {
    const lines = ics.split(/\r?\n/);
    const uidAt = lines.findIndex(l => l.startsWith("UID:" + j.uid + "@"));
    if (uidAt < 0) throw new Error("jednání v hearings.ics není");
    let od = uidAt, do_ = uidAt;
    while (od > 0 && lines[od] !== "BEGIN:VEVENT") od--;
    while (do_ < lines.length && lines[do_] !== "END:VEVENT") do_++;

    // Hlavička = vše před první událostí, bez vlastností celého kalendáře
    // (i s jejich zalomenými pokračováními, která začínají mezerou).
    const head = [];
    let drop = false;
    for (const line of lines.slice(0, lines.indexOf("BEGIN:VEVENT"))) {
      if (line.startsWith(" ")) { if (!drop) head.push(line); continue; }
      drop = ICS_DROP_RE.test(line);
      if (!drop) head.push(line);
    }
    return head.concat(lines.slice(od, do_ + 1), ["END:VCALENDAR"]).join("\r\n") + "\r\n";
  });
}

// „jednani-3-cmo-25-2026-2026-08-17.ics" – bez diakritiky a mezer, ať se
// jméno souboru přenese i tam, kde na ně nejsou zvyklí.
function icsFileName(j) {
  const zaklad = (j.spz || caseName(j)) + " " + (j.datum || "");
  return "jednani-" + zaklad.normalize("NFD").replace(/[\u0300-\u036f]/g, "")
    .replace(/[^0-9A-Za-z]+/g, "-").replace(/^-+|-+$/g, "").toLowerCase() + ".ics";
}

function stahniIcs(j) {
  icsForEvent(j).then(text => {
    const url = URL.createObjectURL(
      new Blob([text], { type: "text/calendar;charset=utf-8" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = icsFileName(j);
    // Kliknutí na pomocný odkaz nesmí probublat na dokument – tam ho čeká
    // posluchač, který zavírá bublinu s detailem, a ta by po stažení zmizela.
    a.addEventListener("click", e => e.stopPropagation());
    document.body.appendChild(a);
    a.click();
    a.remove();
    // Odkaz na blob se ruší až po kliknutí, jinak by stahování nedoběhlo.
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }).catch(e => {
    console.error("Stažení .ics selhalo:", e);
    // Když jednotlivá událost nejde vyříznout, ať člověk dostane aspoň celý kalendář.
    window.open("hearings.ics", "_blank");
  });
}

// Jméno úseku z metadat soudu (scraper ho ukládá vedle období a zdroje
// přehledu). Civilní úsek je většina kalendáře a nepopisuje se; správní
// soudnictví se ukáže, ať je jasné, proč je v IP kalendáři senát „15 A"
// – je to žaloba proti Úřadu průmyslového vlastnictví.
function usekNazev(j) {
  if (!j.usek || j.usek === "civilni") return "";
  const court = (calData.courts || {})[j.soud] || {};
  const meta = (court.useky || {})[j.usek] || {};
  return meta.nazev || j.usek;
}


// Detail jednání do bubliny (ve stylu Google Kalendáře): barva soudu,
// název sporu a kdy, pod tím soud se značkou, síň, předseda a účastníci.
// `datum` a `idx` nese tlačítko na stažení .ics – podle nich si ho
// posluchač zase najde.
function calPopHtml(j, datum, idx) {
  const soud = j.soud === "MS" ? "ms" : "vs";
  const ikona = id => '<svg class="nav-ico" aria-hidden="true"><use href="#' + id + '"></use></svg>';
  let html = '<div class="cal-pop-lista"><button type="button" class="cal-pop-close" aria-label="Zavřít" title="Zavřít">' +
    ikona("icon-x") + "</button></div>" +
    '<div class="cal-pop-hlava"><span class="cal-pop-barva cal-barva-' + soud + '"></span><div>' +
    '<h3 class="cal-pop-titul">' + esc(caseName(j)) + "</h3>" +
    '<div class="cal-pop-kdy">' + esc(denNazev(datum) + (j.hodina ? " · " + j.hodina : "")) + "</div></div></div>";

  const usek = usekNazev(j);
  const sub = [j.spz, usek].filter(Boolean).join(" · ");
  html += '<div class="cal-pop-radky">' + ikona("icon-court") +
    "<div><div>" + esc(courtName(j)) + "</div>" + (sub ? '<div class="cal-pop-sub">' + esc(sub) + "</div>" : "") + "</div>";
  if (j.sin) html += ikona("icon-pin") + "<div>Jednací síň " + esc(j.sin) + "</div>";
  if (j.predseda) html += ikona("icon-user") + "<div>" + esc(j.predseda) + "</div>";
  // Účastníci z dat; starší data je nemají, tak se rozdělí jméno sporu.
  let strany = (j.ucastnici || []).filter(Boolean);
  if (!strany.length && / v\. /.test(j.nazev || "") && !/ a další$/.test(j.nazev)) strany = j.nazev.split(" v. ");
  if (strany.length) html += ikona("icon-users") + "<div>" + strany.map(esc).join("<br>") + "</div>";
  html += "</div>";

  // „Nové jednání" se neukazuje – nové je v kalendáři skoro všechno;
  // zajímavé jsou jen přesuny a změny času nebo síně.
  const zmeny = zmenyJednani(j).filter(z => z.typ !== "nove");
  if (zmeny.length) {
    html += '<div class="cal-pop-zmeny">' + zmeny.map(z =>
      '<span class="cal-zmena">' + esc(zmenaText(z)) + "</span>").join("") + "</div>";
  }
  html += '<div class="cal-pop-akce"><a class="btn btn-hlavni" href="' + esc(infosoudUrl(j)) +
    '" target="_blank" rel="noopener">Otevřít v InfoSoudu ↗</a>';
  // Bez uid (starší data) není co vyříznout – tlačítko se nenabídne.
  if (j.uid) {
    html += '<button type="button" class="btn cal-ics" data-date="' + esc(datum) + '" data-idx="' + idx +
      '" title="Uložit tohle jednání jako událost do vlastního kalendáře">Stáhnout .ics</button>';
  }
  return html + "</div>";
}

function closeCalPop() {
  calPopKey = null;
  const box = document.getElementById("cal-pop");
  if (box) { box.hidden = true; box.innerHTML = ""; }
  document.querySelectorAll(".cal-chip.is-open").forEach(el => {
    el.classList.remove("is-open");
    el.setAttribute("aria-expanded", "false");
  });
}

// Bublina stojí vedle dne: u pondělí až středy vpravo, u čtvrtka a pátku
// vlevo; v prvních dvou týdnech lícuje nahoře se štítkem, v posledním
// dole (roste nahoru). Do okna ji pak ještě dorovnáme.
function placeCalPop(anchor) {
  const box = document.getElementById("cal-pop");
  const den = anchor.closest(".cal-day");
  const s = document.getElementById("cal-shell").getBoundingClientRect();
  const a = anchor.getBoundingClientRect();
  const d = den.getBoundingClientRect();
  const w = box.offsetWidth, h = box.offsetHeight;
  const vpravo = Number(den.dataset.sloupec) < 3;
  let left = vpravo ? d.right - s.left + 8 : d.left - s.left - 8 - w;
  // V úzkém okně by vedle dne nezbylo místo – pak přes mřížku, ale v okně.
  const okno = document.documentElement.clientWidth;
  left = Math.max(8 - s.left, Math.min(left, okno - 8 - s.left - w));
  const nahoru = Number(den.dataset.tyden) >= CAL_TYDNU - 1;
  const top = nahoru ? a.bottom - s.top + 8 - h : a.top - s.top - 8;
  box.style.left = left + "px";
  box.style.top = top + "px";
}

function openCalPop(anchor) {
  const datum = anchor.dataset.date;
  const idx = Number(anchor.dataset.idx);
  const j = (calEventsByDay()[datum] || [])[idx];
  if (!j) return;
  closeCalPop();
  const box = document.getElementById("cal-pop");
  box.innerHTML = calPopHtml(j, datum, idx);
  box.hidden = false;
  placeCalPop(anchor);
  anchor.classList.add("is-open");
  anchor.setAttribute("aria-expanded", "true");
  calPopKey = datum + "#" + idx;
}

function redrawCal() {
  // Překreslením zmizí štítek, ke kterému byla bublina přišpendlená.
  closeCalPop();
  document.getElementById("cal-mesic").hidden = calPohled !== "mesic";
  document.getElementById("cal-seznam").hidden = calPohled !== "seznam";
  document.querySelectorAll("#cal-pohled button").forEach(b =>
    b.setAttribute("aria-pressed", String(b.dataset.pohled === calPohled)));
  renderCalGrid();
  renderCalSeznam();
}

function calShiftDays(delta) {
  const p = new Date(calStart);
  p.setDate(p.getDate() + delta);
  calStart = p;
  redrawCal();
}

function renderKalendar(data) {
  const container = document.getElementById("feed-kalendar");
  if (!data || !Array.isArray(data.jednani)) {
    container.innerHTML = '<p class="feed-empty">Kalendář jednání zatím není k dispozici.</p>';
    return;
  }
  calData = data;
  calStart = calDefaultStart();

  const stitek = (s, nazev) => '<button type="button" class="cal-key cal-key-' + s.toLowerCase() +
    '" data-soud="' + s + '" aria-pressed="true" title="Skrýt nebo zobrazit jednání ' + nazev + '">' +
    '<span class="cal-key-dot"></span><span>' + s + '</span><span class="cal-key-pocet" id="cal-pocet-' + s +
    '"></span></button>';
  container.innerHTML =
    '<div class="cal-toolbar">' +
      '<div class="segment" id="cal-pohled" role="group" aria-label="Zobrazení">' +
        '<button type="button" data-pohled="mesic" aria-pressed="true">3 týdny</button>' +
        '<button type="button" data-pohled="seznam" aria-pressed="false">Seznam</button>' +
      "</div>" +
      // Klikací barevné štítky – legenda mřížky a filtr v jednom.
      stitek("MS", "Městského soudu v Praze") + stitek("VS", "Vrchního soudu v Praze") +
    "</div>" +
    '<div class="cal-mesic" id="cal-mesic">' +
      '<div class="cal-nav">' +
        '<button type="button" id="cal-prev" aria-label="O týden zpět">‹</button>' +
        '<button type="button" id="cal-next" aria-label="O týden vpřed">›</button>' +
        '<span class="cal-title" id="cal-title"></span>' +
        '<button type="button" id="cal-today">Dnes</button>' +
      "</div>" +
      '<div class="cal-shell" id="cal-shell">' +
        '<div class="cal-grid" id="cal-grid"></div>' +
        '<div class="cal-pop" id="cal-pop" role="dialog" aria-label="Detail jednání" hidden></div>' +
      "</div>" +
    "</div>" +
    '<div class="cal-seznam" id="cal-seznam"></div>';

  const info = document.getElementById("cal-info");
  if (info && data.generated) {
    const kdy = czDate(data.generated);
    if (kdy) info.textContent = "Aktualizováno " + kdy + ".";
  }

  document.getElementById("cal-prev")
    .addEventListener("click", () => calShiftDays(-CAL_POSUN_DNU));
  document.getElementById("cal-next")
    .addEventListener("click", () => calShiftDays(CAL_POSUN_DNU));
  document.getElementById("cal-today").addEventListener("click", () => {
    calStart = calDefaultStart();
    redrawCal();
  });
  document.getElementById("cal-pohled").addEventListener("click", e => {
    const b = e.target.closest("button[data-pohled]");
    if (!b) return;
    calPohled = b.dataset.pohled;
    redrawCal();
  });
  container.querySelectorAll(".cal-key").forEach(el => {
    el.addEventListener("click", () => {
      calSoudy[el.dataset.soud] = !calSoudy[el.dataset.soud];
      el.setAttribute("aria-pressed", String(calSoudy[el.dataset.soud]));
      redrawCal();
    });
  });
  container.addEventListener("click", e => {
    const chip = e.target.closest(".cal-chip");
    if (chip) {
      const key = chip.dataset.date + "#" + chip.dataset.idx;
      // Druhý klik na týž štítek bublinu zavře.
      if (calPopKey === key) closeCalPop();
      else openCalPop(chip);
      return;
    }
    if (e.target.closest(".cal-pop-close")) { closeCalPop(); return; }
    const ics = e.target.closest(".cal-ics");
    if (!ics) return;
    const j = (calEventsByDay()[ics.dataset.date] || [])[Number(ics.dataset.idx)];
    if (j) stahniIcs(j);
  });
  // Klik jinam a Esc bublinu zavřou; při změně velikosti okna by stála
  // mimo štítek, tak ji taky zavřeme.
  document.addEventListener("click", e => {
    if (calPopKey && !e.target.closest("#cal-pop, .cal-chip")) closeCalPop();
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && calPopKey) closeCalPop();
  });
  window.addEventListener("resize", () => { if (calPopKey) closeCalPop(); });

  renderCalPocty();
  redrawCal();
}

/* ========== Přihlášení (Clerk) ========== */
// Clerk se načítá až po vykreslení obsahu; web na něj nečeká a při jeho
// výpadku (nebo zablokovaném skriptu) běží dál ve výchozím výběru.
// Publishable key je veřejný a patří do stránky – podle hostitele se volí
// instance. Tajný klíč sem nikdy nepatří (je jen v GitHub secrets).
const CLERK_KLICE = {
  "owl.davidzavada.cz": "pk_live_Y2xlcmsub3dsLmRhdmlkemF2YWRhLmN6JA",  // produkce
  "*": "pk_test_cHJvdmVuLWpheS0zOTI5LmNsZXJrLmFjY291bnRzLmRldiQ"        // náhledy, localhost
};
// Clerk JS (v6) a jeho komponenty (@clerk/ui) z Frontend API instance.
const CLERK_JS = "@clerk/clerk-js@6/dist/clerk.browser.js";
const CLERK_UI = "@clerk/ui@1/dist/ui.browser.js";

let clerk = null;              // načtený Clerk
let clerkStav = "nacitam";     // nacitam | pripraven | nedostupny
let prihlaseny = null;         // id přihlášeného uživatele

function clerkKlic() {
  return CLERK_KLICE[location.hostname] || CLERK_KLICE["*"];
}

// Frontend API instance je v klíči: pk_live_/pk_test_<base64("host$")>.
function clerkFrontendApi(klic) {
  try {
    const host = atob(String(klic).split("_").slice(2).join("_")).replace(/\$$/, "");
    return /^[a-z0-9.-]+$/i.test(host) ? host : "";
  } catch (e) {
    return "";
  }
}

function nactiSkript(src, atributy) {
  return new Promise((resolve, reject) => {
    const el = document.createElement("script");
    Object.keys(atributy || {}).forEach(k => el.setAttribute(k, atributy[k]));
    el.src = src;
    el.async = true;
    el.onload = resolve;
    el.onerror = () => reject(new Error("Nenačetl se " + src));
    document.head.appendChild(el);
  });
}

function initClerk() {
  const klic = clerkKlic();
  const host = clerkFrontendApi(klic);
  if (!host) {
    clerkNedostupny(new Error("neplatný klíč Clerku"));
    return;
  }
  const cdn = "https://" + host + "/npm/";
  Promise.all([
    nactiSkript("vendor/clerk-cs-CZ.js"),
    nactiSkript(cdn + CLERK_UI, { crossorigin: "anonymous" }),
    nactiSkript(cdn + CLERK_JS, { crossorigin: "anonymous", "data-clerk-publishable-key": klic })
  ])
    .then(() => window.Clerk.load({
      ui: { ClerkUI: window.__internal_ClerkUICtor },
      localization: window.clerkCsCZ
    }))
    .then(() => {
      clerk = window.Clerk;
      clerkStav = "pripraven";
      clerk.addListener(({ user }) => zmenaUctu(user));
      zmenaUctu(clerk.user, true);
      // Stránka otevřená s kotvou #nastaveni: dialog, jakmile je znám účet.
      if (vyberZKotvy) {
        vyberZKotvy = false;
        if (prihlaseny) otevriVyber();
      }
    })
    .catch(clerkNedostupny);
}

function clerkNedostupny(e) {
  console.warn("Přihlášení není k dispozici:", e);
  clerkStav = "nedostupny";
  vykresliUcet();
  vykresliNastaveni();
}

// Výběr se přepíná jen při přihlášení a odhlášení (jiný účet). Změny
// výběru dělá tahle stránka sama, takže pozdější ozvěny od Clerku po
// uložení nic nepřepisují. `vzdy` = první stav po načtení Clerku.
function zmenaUctu(user, vzdy) {
  const id = user ? user.id : null;
  if (vzdy || id !== prihlaseny) {
    prihlaseny = id;
    vyber = user ? normalizujVyber((user.unsafeMetadata || {}).owl) : vychoziVyber();
    // Nový účet začíná na první záložce a bez rozpracovaných změn.
    vyberTab = "oblasti";
    koncept = null;
    stavUlozeni = "";
    const dialog = document.getElementById("vyber-dialog");
    if (dialog && dialog.open) dialog.close();
    vykresliZdroje();
    vykresliNastaveni();
    if (user) uvitatNovy(user);
  }
  vykresliUcet();
}

// Po registraci se Můj výběr otevře sám – jen u čerstvě založeného účtu bez
// uloženého výběru a jen jednou (příznak v localStorage). Průvodce není
// potřeba: stačí okno s výchozím výběrem a jednou větou nahoře.
const UVITANI_MIN = 15;

function uvitatNovy(user) {
  if ((user.unsafeMetadata || {}).owl) return;
  const zalozen = user.createdAt ? new Date(user.createdAt).getTime() : NaN;
  if (!(Date.now() - zalozen < UVITANI_MIN * 60 * 1000)) return;
  const klic = "owl:uvitani:" + user.id;
  try {
    if (localStorage.getItem(klic)) return;
    localStorage.setItem(klic, "1");
  } catch (e) { /* bez úložiště se okno otevře, jen si to nezapamatuje */ }
  uvitani = true;
  vyberTab = "oblasti";
  otevriVyber();
}

function prihlasit(registrace) {
  if (!clerk) return;
  const zpet = { fallbackRedirectUrl: location.href };
  if (registrace) clerk.openSignUp(zpet);
  else clerk.openSignIn(zpet);
}

// Hlavička: nepřihlášený má „Přihlásit se" a „Registrace", přihlášený
// odkaz na svůj výběr a tlačítko účtu od Clerku.
function vykresliUcet() {
  const el = document.getElementById("ucet");
  if (!el) return;
  if (clerkStav !== "pripraven") {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  const tlacitko = el.querySelector(".ucet-user");
  if (prihlaseny) {
    if (tlacitko) return;
    el.innerHTML = '<span class="ucet-user"></span>';
    // Můj výběr je první položka nabídky účtu (před správou účtu a odhlášením).
    clerk.mountUserButton(el.querySelector(".ucet-user"), {
      customMenuItems: [
        { label: "Můj výběr", onClick: otevriVyber,
          mountIcon: node => { node.innerHTML = '<svg class="nav-ico ucet-menu-ico" aria-hidden="true">' +
            '<use href="#icon-sliders"></use></svg>'; },
          unmountIcon: node => { if (node) node.innerHTML = ""; } },
        { label: "manageAccount" },
        { label: "signOut" }
      ]
    });
  } else if (!el.querySelector(".ucet-prihlasit")) {
    if (tlacitko) clerk.unmountUserButton(tlacitko);
    el.innerHTML = '<button type="button" class="ucet-btn ucet-prihlasit">Přihlásit se</button>' +
      '<button type="button" class="ucet-btn ucet-btn-hlavni ucet-registrace">Registrace</button>';
  }
}

/* ========== Nastavení výběru (#nastaveni) ========== */

const KOLEGIA = { civilni: "Občanskoprávní a obchodní kolegium", trestni: "Trestní kolegium" };

// Senáty NS k výběru: ze seznamu a navíc ty, které jsou v datech nebo ve
// výběru a v seznamu chybí (aspoň s číslem).
function senatyKVyberu(v) {
  const znam = SENATY_NS.map(s => s.senat);
  const r = zdrojeVysledky && zdrojeVysledky[FEEDS.findIndex(f => f.key === "nsoud")];
  const vDatech = r && r.status === "fulfilled" ? r.value.map(i => i.senat) : [];
  const navic = bezDuplicit(vDatech.concat((v || vyber).ns.senaty))
    .filter(n => Number.isInteger(n) && znam.indexOf(n) < 0)
    .sort((a, b) => a - b)
    .map(n => ({ senat: n, kolegium: "ostatni", popis: "" }));
  return SENATY_NS.concat(navic);
}

// Registr časopisů z data/casopisy.json: štítky v nastavení a seznam
// v nápovědě karty.
function nastavCasopisy(seznam) {
  CASOPISY = seznam.filter(c => c && c.id && c.zkratka);
  const ul = document.getElementById("casopisy-seznam");
  if (ul) {
    ul.innerHTML = CASOPISY.map(c => "<li>" + esc(c.zkratka === c.nazev ? c.nazev : c.zkratka + " – " + c.nazev) +
      (c.vydavatel ? ' <span class="legend">(' + esc(c.vydavatel) + ")</span>" : "") + "</li>").join("");
  }
}

// Časopisy k výběru: všechny z registru (id, zkratka, název).
function casopisyKVyberu() {
  return CASOPISY.slice();
}


// Dialog výběru. Vlevo záložky (na telefonu přepínač nahoře) se souhrnem
// toho, co je v nich vybrané; vpravo seznam se zaškrtávacími poli. Změny
// se sbírají v konceptu a platí – na webu i v účtu – až po „Uložit".
let vyberTab = "oblasti";         // oblasti | senaty | casopisy
let uvitani = false;              // dialog otevřený po registraci – nahoře uvítací věta
let otevrenoZ = null;             // prvek, na který se po zavření vrátí fokus
let koncept = null;               // rozpracovaný výběr; null = stejný jako uložený
let stavUlozeni = "";             // „Ukládám…", chyba
let ukladam = false;

function kopie(v) {
  return JSON.parse(JSON.stringify(v));
}

function rozpracovano() {
  return !!koncept && JSON.stringify(koncept) !== JSON.stringify(vyber);
}


// Zaškrtávací pole: celý řádek je tlačítko s role="checkbox". Stav
// „mixed" zbyde jen po dřívějším výběru oblastí zvlášť pro jednotlivé soudy.
function volbaHtml(atributy, stav, obsah, title, trida) {
  const attrs = Object.keys(atributy).map(k => " " + k + '="' + esc(atributy[k]) + '"').join("");
  return '<button type="button" role="checkbox" class="volba' + (trida ? " " + trida : "") + '"' + attrs +
    ' aria-checked="' + stav + '"' + (title ? ' title="' + esc(title) + '"' : "") +
    '><span class="volba-box" aria-hidden="true"><svg><use href="#icon-check"></use></svg></span>' + obsah + "</button>";
}

// Skupina voleb: nadpis, počet vybraných a vpravo „Vybrat vše / Zrušit vše".
function skupinaHtml(nazev, pocet, tlacitko, obsah, trida) {
  return '<div class="vyber-skupina"><div class="vyber-skupina-hlava' + (trida ? " " + trida : "") + '">' +
    '<span class="vyber-skupina-nazev">' + esc(nazev) + "</span>" +
    (pocet ? '<span class="vyber-skupina-pocet">' + pocet + "</span>" : "") + (tlacitko || "") + "</div>" +
    obsah + "</div>";
}

function vseHtml(atributy, vse) {
  const attrs = Object.keys(atributy).map(k => " " + k + '="' + esc(atributy[k]) + '"').join("");
  return '<button type="button" class="vyber-vse"' + attrs + ">" + (vse ? "Zrušit vše" : "Vybrat vše") + "</button>";
}

function skupinyOblasti() {
  const skupiny = [];
  OBLASTI_SEZNAM.forEach(o => {
    let sk = skupiny.find(x => x.nazev === o.skupina);
    if (!sk) skupiny.push(sk = { nazev: o.skupina, oblasti: [] });
    sk.oblasti.push(o);
  });
  return skupiny;
}

function soudyOblasti(v, id) {
  return SOUDY_VYBERU.filter(s => v[s.soud].oblasti.indexOf(id) >= 0);
}

// Oblast se vybírá pro všechny soudy najednou. „mixed" zbyde jen po
// dřívějším výběru zvlášť pro jednotlivé soudy.
function stavOblasti(v, id) {
  const n = soudyOblasti(v, id).length;
  return n === SOUDY_VYBERU.length ? "true" : n ? "mixed" : "false";
}


function oblastiHtml(v) {
  return '<p class="vyber-uvod">Platí pro všechny soudy. Rozhodnutí se ukáže, když ho AI zařadí aspoň do ' +
    "jedné z vybraných oblastí.</p>" +
    skupinyOblasti().map((sk, i) => {
      const vybrano = sk.oblasti.filter(o => stavOblasti(v, o.id) === "true").length;
      return skupinaHtml(sk.nazev, vybrano + " z " + sk.oblasti.length,
        vseHtml({ "data-skupina": i }, vybrano === sk.oblasti.length),
        '<div class="volby">' + sk.oblasti.map(o => {
          const stav = stavOblasti(v, o.id);
          const title = stav === "mixed"
            ? "Vybráno jen u některých soudů (" + soudyOblasti(v, o.id).map(s => s.zkratka).join(", ") +
              "), kliknutím u všech"
            : o.popis;
          return volbaHtml({ "data-oblast": o.id }, stav, '<span class="volba-nazev">' + esc(o.nazev) + "</span>", title);
        }).join("") + "</div>");
    }).join("");
}

// Senáty NS. Občanskoprávní jednotlivě; trestní kolegium jediným
// vypínačem pro všechny jeho senáty. Senát, který je v datech a v seznamu
// chybí, se nabídne aspoň s číslem.
function senatyHtml(v) {
  const skupiny = {};
  senatyKVyberu(v).forEach(s => { (skupiny[s.kolegium] = skupiny[s.kolegium] || []).push(s); });
  const volba = s => volbaHtml({ "data-senat": s.senat }, String(v.ns.senaty.indexOf(s.senat) >= 0),
    '<span class="volba-cislo">' + s.senat + '</span><span class="volba-popis">' + esc(s.popis || "") + "</span>",
    "", "volba-senat");
  let html = '<p class="vyber-uvod">Z vybraných senátů Nejvyššího soudu se ukáže všechno, bez ohledu na oblast. ' +
    "Z ostatních jen to, co spadá do vašich oblastí.</p>";
  if (skupiny.civilni) {
    const vybrane = skupiny.civilni.filter(s => v.ns.senaty.indexOf(s.senat) >= 0).length;
    html += skupinaHtml(KOLEGIA.civilni, vybrane + " z " + skupiny.civilni.length, "",
      '<div class="volby">' + skupiny.civilni.map(volba).join("") + "</div>", "bez-hlavy-m");
  }
  const trestni = senatyKolegia("trestni");
  if (trestni.length) {
    html += skupinaHtml(KOLEGIA.trestni, "", "",
      '<button type="button" class="volba-prepinac" role="switch" aria-checked="' + celeKolegium(v, "trestni") +
      '" data-kolegium="trestni"><span class="volba-text">' +
      '<span class="volba-titul"><span class="jen-d">Všechna rozhodnutí trestních senátů</span>' +
      '<span class="jen-m">Trestní kolegium</span></span>' +
      '<span class="volba-popis"><span class="jen-d">' + esc((trestni.length > 1 ? "senáty " : "senát ") + seznamCisel(trestni)) +
      '</span><span class="jen-m">všechna rozhodnutí trestních senátů</span></span></span>' +
      '<span class="switch" aria-hidden="true"></span></button>', "bez-hlavy-m");
  }
  if (skupiny.ostatni) {
    html += skupinaHtml("Další senáty", "", "", '<div class="volby">' + skupiny.ostatni.map(volba).join("") + "</div>");
  }
  return html;
}

function souhrnSenatu(v) {
  const trestni = celeKolegium(v, "trestni") ? senatyKolegia("trestni") : [];
  const cisla = v.ns.senaty.filter(n => trestni.indexOf(n) < 0).sort((a, b) => a - b);
  let text = cisla.length ? (cisla.length === 1 ? "senát " : "senáty ") + cisla.join(", ") : "";
  if (trestni.length) text += (text ? " + " : "") + "trestní";
  return text || "žádný senát";
}

// Časopisy: vybraný se ukazuje (ukládá se, které jsou skryté).
function casopisyHtml(v, casopisy) {
  const vybrane = casopisy.filter(c => v.skryte_casopisy.indexOf(c.id) < 0).length;
  return '<p class="vyber-uvod">Nová čísla a články za poslední 4 týdny z vybraných časopisů.</p>' +
    skupinaHtml("Časopisy", vybrane + " z " + casopisy.length,
      vseHtml({ "data-vse": "casopisy" }, vybrane === casopisy.length),
      '<div class="volby">' + casopisy.map(c => volbaHtml({ "data-casopis": c.id },
        String(v.skryte_casopisy.indexOf(c.id) < 0), '<span class="volba-nazev">' + esc(c.nazev || c.zkratka) + "</span>",
        c.zkratka !== c.nazev ? c.zkratka : "", "volba-casopis")).join("") + "</div>");
}

function tvar(n, jeden, dva, pet) {
  return n + " " + (n === 1 ? jeden : n >= 2 && n <= 4 ? dva : pet);
}

function souhrnOblasti(v) {
  const vse = OBLASTI_SEZNAM.filter(o => stavOblasti(v, o.id) === "true").length;
  const cast = OBLASTI_SEZNAM.filter(o => stavOblasti(v, o.id) === "mixed").length;
  if (!vse && !cast) return "žádná";
  return vse + " z " + OBLASTI_SEZNAM.length + (cast ? ", " + cast + " jen u některých soudů" : "");
}

// Senáty NS. Občanskoprávní jednotlivě, pod sebou (ve sloupcích shora
// dolů); trestní kolegium jediným vypínačem pro všechny jeho senáty.
// Senát, který je v datech a v seznamu chybí, se nabídne čipem.
function senatyKolegia(k) {
  return SENATY_NS.filter(s => s.kolegium === k).map(s => s.senat);
}

function celeKolegium(v, k) {
  const senaty = senatyKolegia(k);
  return senaty.length > 0 && senaty.every(n => v.ns.senaty.indexOf(n) >= 0);
}

function seznamCisel(cisla) {
  return cisla.length > 1 ? cisla.slice(0, -1).join(", ") + " a " + cisla[cisla.length - 1] : String(cisla[0]);
}

function souhrnCasopisu(v, casopisy) {
  const skryte = casopisy.filter(c => v.skryte_casopisy.indexOf(c.id) >= 0);
  if (!skryte.length) return "všech " + casopisy.length;
  if (skryte.length === casopisy.length) return "žádný";
  return (casopisy.length - skryte.length) + " z " + casopisy.length;
}


function akceHtml() {
  const zmeny = rozpracovano();
  return '<div class="vyber-akce" id="vyber-akce">' +
    '<button type="button" class="btn" id="vyber-vychozi">Obnovit výchozí</button>' +
    '<span class="vyber-stav" id="vyber-stav" role="status">' +
    esc(stavUlozeni || (zmeny ? "Neuložené změny" : "")) + "</span>" +
    '<button type="button" class="btn" id="vyber-zrusit">Zrušit</button>' +
    '<button type="button" class="btn btn-hlavni" id="vyber-ulozit"' +
    (zmeny && !ukladam ? "" : " disabled") + ">Uložit</button></div>";
}

function vykresliNastaveni() {
  const el = document.getElementById("nastaveni-obsah");
  if (!el) return;
  // Dialog se otevírá jen přihlášenému (z nabídky účtu); jinak je prázdný.
  if (clerkStav !== "pripraven" || !prihlaseny) {
    el.innerHTML = "";
    return;
  }
  if (!koncept) koncept = kopie(vyber);
  const v = koncept;
  const casopisy = casopisyKVyberu();
  const taby = [
    ["oblasti", "Oblasti práva", "Oblasti", souhrnOblasti(v)],
    ["senaty", "Senáty NS", "Senáty", souhrnSenatu(v)]
  ];
  if (casopisy.length) taby.push(["casopisy", "Časopisy", "Časopisy", souhrnCasopisu(v, casopisy)]);
  if (!taby.some(t => t[0] === vyberTab)) vyberTab = "oblasti";
  const panel = vyberTab === "senaty" ? senatyHtml(v)
    : vyberTab === "casopisy" ? casopisyHtml(v, casopisy) : oblastiHtml(v);
  // Posunutí seznamu přežije překreslení po kliknutí na volbu.
  const staryObsah = el.querySelector(".vyber-obsah");
  const posun = staryObsah && staryObsah.dataset.tab === vyberTab ? staryObsah.scrollTop : 0;
  el.innerHTML = '<div class="nastaveni"><div class="vyber-taby-lista">' +
    '<div class="vyber-taby" role="tablist" aria-label="Části výběru">' +
    taby.map(([k, label, kratce, souhrn]) => {
      const vybrana = k === vyberTab;
      return '<button type="button" role="tab" class="vyber-tab" id="tab-' + k + '" data-tab="' + k +
        '" aria-selected="' + vybrana + '" aria-controls="vyber-obsah" tabindex="' + (vybrana ? 0 : -1) + '">' +
        '<span class="tab-label">' + label + '</span><span class="tab-souhrn">' + esc(souhrn) + "</span>" +
        '<span class="tab-short" aria-hidden="true">' + kratce + "</span></button>";
    }).join("") + "</div></div>" +
    '<div class="vyber-obsah" id="vyber-obsah" role="tabpanel" aria-labelledby="tab-' + vyberTab +
    '" data-tab="' + vyberTab + '" tabindex="-1">' +
    (uvitani && vyberTab === "oblasti" ? '<p class="nastaveni-uvitani">Vítejte v Owl. Vyberte si, co chcete ' +
      "sledovat – výchozí je IP a IT. Výběr kdykoli změníte v nabídce účtu.</p>" : "") +
    panel + "</div>" + akceHtml() + "</div>";
  el.querySelector(".vyber-obsah").scrollTop = posun;
  hlidejOdchod();
}

// Jen lišta s tlačítky (stav ukládání) – zbytek stránky se nemění.
function obnovAkce() {
  const lista = document.getElementById("vyber-akce");
  if (lista) lista.outerHTML = akceHtml();
  hlidejOdchod();
}

// Neuložené změny by zavřením nebo obnovením stránky zmizely. Posluchač
// beforeunload visí jen, když nějaké jsou – stálý by prohlížeči bránil
// uložit stránku pro návrat zpět (bfcache).
let strazAktivni = false;

function strazOdchodu(e) {
  e.preventDefault();
  e.returnValue = "";
}

function hlidejOdchod() {
  const treba = rozpracovano();
  if (treba === strazAktivni) return;
  strazAktivni = treba;
  if (treba) window.addEventListener("beforeunload", strazOdchodu);
  else window.removeEventListener("beforeunload", strazOdchodu);
}

// Seznamy ve výběru drží stálé pořadí (oblasti podle seznamu, senáty podle
// čísla, časopisy podle registru) – výběr, který se proklikáním vrátí
// k uloženému nebo výchozímu, se pak jako takový i pozná.
function seradVyber(v) {
  const poradi = (seznam, klic) => (a, b) =>
    seznam.findIndex(x => x[klic] === a) - seznam.findIndex(x => x[klic] === b);
  SOUDY_VYBERU.forEach(s => v[s.soud].oblasti.sort(poradi(OBLASTI_SEZNAM, "id")));
  v.ns.senaty.sort((a, b) => a - b);
  v.skryte_casopisy.sort(poradi(CASOPISY, "id"));
  return v;
}

function nastavPolozku(seznam, x, zapnout) {
  const i = seznam.indexOf(x);
  if (zapnout && i < 0) seznam.push(x);
  else if (!zapnout && i >= 0) seznam.splice(i, 1);
}

function prepniOblasti(v, ids) {
  const zapnout = !ids.every(id => stavOblasti(v, id) === "true");
  ids.forEach(id => SOUDY_VYBERU.forEach(s => nastavPolozku(v[s.soud].oblasti, id, zapnout)));
}

// Uložení: koncept se zapíše do účtu a teprve po úspěchu začne platit na
// webu. Výchozí výběr se neukládá – z účtu se `owl` smaže.
function ulozVyber() {
  const user = clerk && clerk.user;
  if (!user || !koncept || ukladam) return;
  const novy = kopie(koncept);
  const meta = Object.assign({}, user.unsafeMetadata);
  if (jeVychozi(novy)) delete meta.owl;
  else meta.owl = novy;
  ukladam = true;
  stavUlozeni = "Ukládám…";
  obnovAkce();
  user.update({ unsafeMetadata: meta }).then(() => {
    ukladam = false;
    vyber = novy;
    koncept = null;
    stavUlozeni = "";
    vykresliZdroje();
    const dialog = document.getElementById("vyber-dialog");
    if (dialog && dialog.open) dialog.close();
  }, () => {
    ukladam = false;
    stavUlozeni = "Nepodařilo se uložit – zkuste to znovu.";
    obnovAkce();
  });
}


// Po změně se dialog vykreslí znovu (souhrny, stavy voleb, lišta);
// fokus zůstane na prvku, na který se klikalo.
function prekresli(el, t) {
  const klic = ["tab", "oblast", "skupina", "senat", "kolegium", "casopis", "vse"].find(k => t.dataset[k] !== undefined);
  const selektor = klic ? "[data-" + klic + '="' + CSS.escape(t.dataset[klic]) + '"]' : (t.id ? "#" + t.id : "");
  vykresliNastaveni();
  const cil = selektor && el.querySelector(selektor);
  if (cil && !cil.disabled && !cil.hidden) cil.focus({ preventScroll: true });
}

// Dialog Můj výběr: otevře se s uloženým výběrem; nepřihlášenému nabídne
// přihlášení. Zavřít s neuloženými změnami jde až po potvrzení.
function otevriVyber() {
  const dialog = document.getElementById("vyber-dialog");
  if (!dialog) return;
  if (clerkStav !== "pripraven") return;
  if (!prihlaseny) {
    prihlasit(false);
    return;
  }
  koncept = null;
  stavUlozeni = "";
  vykresliNastaveni();
  if (!dialog.open) {
    otevrenoZ = document.activeElement;
    dialog.showModal();
    // Fokus na seznam, ne na křížek – ten by jinak svítil rámečkem.
    const obsah = dialog.querySelector(".vyber-obsah");
    if (obsah) obsah.focus({ preventScroll: true });
  }
}

function zavriVyber() {
  const dialog = document.getElementById("vyber-dialog");
  if (!dialog || !dialog.open) return;
  if (rozpracovano() && !window.confirm("Výběr má neuložené změny. Zavřít bez uložení?")) return;
  koncept = null;
  stavUlozeni = "";
  dialog.close();
}


// „Zrušit" zahodí rozpracované změny bez ptaní – je to výslovná volba.
function zrusVyber() {
  const dialog = document.getElementById("vyber-dialog");
  koncept = null;
  stavUlozeni = "";
  if (dialog && dialog.open) dialog.close();
}

function initNastaveni() {
  const el = document.getElementById("nastaveni-obsah");
  const dialog = document.getElementById("vyber-dialog");
  document.addEventListener("click", e => {
    if (e.target.closest(".ucet-prihlasit")) prihlasit(false);
    else if (e.target.closest(".ucet-registrace")) prihlasit(true);
    else if (e.target.closest('a[href="#nastaveni"]')) {
      e.preventDefault();
      otevriVyber();
    }
  });
  if (dialog) {
    // Esc, křížek i klik vedle dialogu (na jeho pozadí) zavírají stejně.
    dialog.addEventListener("cancel", e => { e.preventDefault(); zavriVyber(); });
    dialog.addEventListener("click", e => { if (e.target === dialog) zavriVyber(); });
    const zavrit = document.getElementById("vyber-zavrit");
    if (zavrit) zavrit.addEventListener("click", zavriVyber);
    // Po zavření fokus tam, odkud se dialog otevřel; položka z nabídky účtu
    // mezitím zmizí, pak na tlačítko účtu.
    dialog.addEventListener("close", () => {
      uvitani = false;
      koncept = null;
      hlidejOdchod();
      const videt = x => x && x.isConnected && x !== document.body && x.getClientRects().length > 0;
      const cil = videt(otevrenoZ) ? otevrenoZ
        : document.querySelector("#ucet .cl-userButtonTrigger, #ucet button");
      otevrenoZ = null;
      if (cil && typeof cil.focus === "function") cil.focus();
    });
  }
  if (!el) return;
  // Záložky se přepínají i šipkami (vzor ARIA Tabs).
  el.addEventListener("keydown", e => {
    const tab = e.target.closest && e.target.closest(".vyber-tab");
    if (!tab) return;
    const vsechny = Array.from(el.querySelectorAll(".vyber-tab"));
    const i = vsechny.indexOf(tab);
    const krok = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 }[e.key];
    let cil = null;
    if (krok) cil = vsechny[(i + krok + vsechny.length) % vsechny.length];
    else if (e.key === "Home") cil = vsechny[0];
    else if (e.key === "End") cil = vsechny[vsechny.length - 1];
    if (!cil) return;
    e.preventDefault();
    vyberTab = cil.dataset.tab;
    prekresli(el, cil);
  });
  el.addEventListener("click", e => {
    const t = e.target.closest("button");
    if (!t || !el.contains(t) || !koncept) return;
    const d = t.dataset;
    if (d.tab) {
      vyberTab = d.tab;
      prekresli(el, t);
      return;
    }
    if (t.id === "vyber-ulozit") {
      ulozVyber();
      return;
    }
    if (t.id === "vyber-zrusit") {
      zrusVyber();
      return;
    }
    if (ukladam) return;
    if (t.id === "vyber-vychozi") koncept = vychoziVyber();
    else if (d.oblast) prepniOblasti(koncept, [d.oblast]);
    else if (d.skupina !== undefined) {
      prepniOblasti(koncept, skupinyOblasti()[Number(d.skupina)].oblasti.map(o => o.id));
    } else if (d.senat) {
      const n = Number(d.senat);
      nastavPolozku(koncept.ns.senaty, n, koncept.ns.senaty.indexOf(n) < 0);
    } else if (d.casopis) {
      nastavPolozku(koncept.skryte_casopisy, d.casopis, koncept.skryte_casopisy.indexOf(d.casopis) < 0);
    } else if (d.vse === "casopisy") {
      const ids = casopisyKVyberu().map(c => c.id);
      const vse = ids.every(id => koncept.skryte_casopisy.indexOf(id) < 0);
      koncept.skryte_casopisy = vse ? ids : [];
    } else if (d.kolegium) {
      const zapnout = !celeKolegium(koncept, d.kolegium);
      senatyKolegia(d.kolegium).forEach(n => nastavPolozku(koncept.ns.senaty, n, zapnout));
    } else return;
    seradVyber(koncept);
    stavUlozeni = "";
    prekresli(el, t);
  });
}

/* ========== Stránky a navigace ========== */
// Obsah je rozdělený na stránky; přepíná se podle adresy (#kotva).
// Kotvy sekcí zůstávají platné – odkaz na #dnesni otevře Novinky.
// Dvoutýdenní přehled i každý zdroj mají vlastní stránku.
const PAGES = [
  { id: "prehled",  sections: ["dnesni"] },
  { id: "dvatydny", sections: [] },
  { id: "nsoud",    sections: [] },
  { id: "nss",      sections: [] },
  { id: "us",       sections: [] },
  { id: "sdeu",     sections: [] },
  { id: "casopisy", sections: [] },
  { id: "kalendar", sections: ["jednani"] }
];
// Kotvy z uložených odkazů: dřív byly všechny zdroje na jedné stránce
// „Všechno nové" a SDEU se jmenoval cjeu. Můj výběr byl stránkou #nastaveni –
// teď je to dialog, kotva ho otevře nad Novinkami.
const STARE_KOTVY = { recentni: "nsoud", cjeu: "sdeu", nastaveni: "prehled" };
let vyberZKotvy = false;

let currentPage = PAGES[0];
let updateNav = function () {};

function pageOf(hash) {
  const id = String(hash || "").replace(/^#/, "");
  return PAGES.find(p => p.id === id || p.sections.indexOf(id) >= 0) || PAGES[0];
}

// Přepne na stránku a odscrolluje – buď na sekci, nebo na začátek stránky.
function navigate(hash, push) {
  let id = String(hash || "").replace(/^#/, "");
  if (id === "nastaveni") {
    // Dialog potřebuje Clerk – když ještě není, otevře se po přihlášení.
    if (clerkStav === "pripraven") otevriVyber();
    else vyberZKotvy = true;
  }
  if (STARE_KOTVY[id]) {
    id = STARE_KOTVY[id];
    history.replaceState(null, "", "#" + id);
  }
  const page = pageOf(id);
  currentPage = page;
  PAGES.forEach(p => {
    const el = document.getElementById(p.id);
    if (el) el.hidden = (p !== page);
  });

  const section = page.sections.indexOf(id) >= 0 ? document.getElementById(id) : null;
  if (section) section.scrollIntoView({ block: "start" });
  else window.scrollTo(0, 0);

  if (push) {
    history.pushState(null, "", "#" + (id || page.id));
    const nadpis = document.querySelector("#" + page.id + " .page-title");
    if (nadpis) {
      nadpis.setAttribute("tabindex", "-1");
      nadpis.focus({ preventScroll: true });
    }
  }
  updateNav();
  odhalZalozku();
}

// V úzkém okně se přepínač stránek posouvá do strany: aktivní záložka
// musí být vidět a u okraje, za kterým jsou další záložky, text vybledne.
function odhalZalozku() {
  const tabs = document.querySelector(".pagetabs");
  const a = tabs && tabs.querySelector("a.active");
  if (!a || tabs.scrollWidth <= tabs.clientWidth) return;
  const t = tabs.getBoundingClientRect();
  const r = a.getBoundingClientRect();
  if (r.left < t.left + 28) tabs.scrollLeft -= t.left + 28 - r.left;
  else if (r.right > t.right - 28) tabs.scrollLeft += r.right - (t.right - 28);
}

function initPagetabs() {
  const tabs = document.querySelector(".pagetabs");
  if (!tabs) return;
  const okraje = () => {
    const max = tabs.scrollWidth - tabs.clientWidth;
    tabs.classList.toggle("dalsi-vlevo", tabs.scrollLeft > 2);
    tabs.classList.toggle("dalsi-vpravo", tabs.scrollLeft < max - 2);
  };
  tabs.addEventListener("scroll", okraje, { passive: true });
  window.addEventListener("resize", okraje);
  okraje();
}

function initNav() {
  const links = Array.from(document.querySelectorAll("#sidenav a, .pagetabs a"));

  function update() {
    // Sekce se hledají až tady – po přepnutí stránky jsou vidět jiné.
    const targets = currentPage.sections
      .map(id => ({ id, el: document.getElementById(id) }))
      .filter(t => t.el);
    let current = targets[0];
    targets.forEach(t => {
      if (t.el.getBoundingClientRect().top <= 140) current = t;
    });
    // U konce stránky zvýrazni poslední položku (sekce dole se nemusí
    // doscrollovat až k hornímu okraji okna).
    if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4) {
      current = targets[targets.length - 1];
    }
    const active = current ? current.id : "";
    links.forEach(a => {
      const href = String(a.getAttribute("href") || "").replace(/^#/, "");
      // Zvýrazněná je aktuální sekce a k ní i její stránka.
      a.classList.toggle("active", href === active || href === currentPage.id);
      if (href === currentPage.id) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
  }

  links.forEach(a => a.addEventListener("click", e => {
    // Skok si řešíme sami – cílová sekce může být na skryté stránce.
    e.preventDefault();
    navigate(a.getAttribute("href"), true);
  }));
  // Logo vede na Novinky (na začátek stránky); když už tam jsme,
  // nový záznam do historie nepřidá.
  const znacka = document.querySelector(".znacka");
  if (znacka) znacka.addEventListener("click", e => {
    e.preventDefault();
    navigate("#prehled", location.hash !== "#prehled");
  });
  window.addEventListener("popstate", () => navigate(location.hash, false));
  document.addEventListener("scroll", update, { passive: true });
  updateNav = update;
  initPagetabs();
}

// Datum poslední aktualizace = nejnovější aktualizace ze všech zdrojů
// (`generated` v JSONu).
function showUpdated() {
  const latest = FEEDS.map(f => new Date(f.aktualizovano || ""))
    .filter(d => !isNaN(d)).sort((a, b) => b - a)[0];
  if (!latest) return;
  // „Aktualizováno dnes 2:14", jinak s datem.
  const cas = latest.getHours() + ":" + String(latest.getMinutes()).padStart(2, "0");
  const dnes = isoOf(latest) === isoOf(new Date());
  document.getElementById("updated").textContent = "Aktualizováno " +
    (dnes ? "dnes" : latest.getDate() + ". " + (latest.getMonth() + 1) + ".") + " " + cas;
}

function initApp() {
  // Prohlížeč se po refreshi snaží obnovit pozici scrollu, jenže obsah tu
  // dorazí až po fetchi – stránka by se pod ním nafoukla a poskočila.
  // Necháme si scroll pod kontrolou a začínáme nahoře.
  if ("scrollRestoration" in history) history.scrollRestoration = "manual";

  // Každý zdroj stáhneme jen jednou a sdílíme mezi sekcemi (všechno / nové).
  // Výchozí výběr (oblasti, senáty NS) se musí znát dřív, než se filtruje.
  const vyberPromise = Promise.all([
    fetchJson("data/oblasti.json").catch(() => null),
    fetchJson("data/ns_senaty.json").catch(() => null)
  ]).then(([oblasti, senaty]) => nastavVyber(oblasti, senaty));
  // Filtruje se až při vykreslení (vykresliZdroje) – výběr se po přihlášení
  // může změnit a data se kvůli tomu znovu nestahují.
  const feedPromises = FEEDS.map(f => vyberPromise.then(() => nactiZdroj(f)));
  // Dvoutýdenní přehled a kalendář se generují zvlášť – když chybí, jen se
  // nevykreslí; zbytek stránky na ně nečeká déle než na feedy.
  const digestPromise = fetchJson("digest.json").catch(() => null);
  const hearingsPromise = fetchJson("hearings.json").catch(() => null);
  initNav();
  initNovinkyFiltr();
  initNastaveni();

  // Vykreslíme až všechny feedy dorazí (stahují se paralelně, jsou ze
  // stejného původu). Jedno překreslení místo tří – stránka při načítání
  // neposkakuje. Selhání jednoho feedu ostatní nezdrží.
  // Čekáme i na fonty, ať text po odkrytí nepřeskočí na jiné písmo.
  const fontsReady = document.fonts ? document.fonts.ready.catch(() => {}) : Promise.resolve();
  Promise.all([Promise.allSettled(feedPromises), digestPromise, hearingsPromise, fontsReady])
    .then(([results, digest, hearings]) => {
      zdrojeVysledky = results;
      ukazOkna();
      vykresliZdroje();
      vykresliNastaveni();
      renderDigest(digest);
      renderKalendar(hearings);
      // Až teď je jasná výška stránky – otevřeme kotvu z adresy (a přepočítáme
      // zvýraznění) ještě než stránku odkryjeme, ať nic nepřeskočí.
      navigate(location.hash, false);
      document.documentElement.classList.remove("is-loading");
      // Přihlášení až teď – obsah na Clerk nečeká.
      initClerk();
    });

  Promise.allSettled(feedPromises).then(showUpdated);
}

initApp();