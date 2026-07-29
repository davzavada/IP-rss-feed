/* Právní RSS – čtečka nad statickými XML feedy.
   Obsah:
     1. Konfigurace zdrojů      2. Úložiště nastavení a přečtených položek
     3. Pomocné funkce a ikony  4. Načtení a normalizace feedů
     5. Výběr pohledu           6. Sidebar (sbírky + hesla)
     7. Seznam položek          8. Detail, citace a RIS
     9. Události a klávesnice   10. Dělítka panelů    11. Start           */
"use strict";

/* ---------- 1. Konfigurace zdrojů ---------- */
const COLLECTIONS = [
  { id: "nsoud", label: "NS ČR · 23 Cdo", feed: "feed.xml", archive: "feed_archive.xml",
    kindLabel: "Rozhodnutí NS ČR – senát 23 Cdo", window: "1 týden" },
  { id: "cjeu", label: "CJEU · IP / IT", feed: "ipcuria_feed.xml", archive: "ipcuria_archive.xml",
    kindLabel: "Soudní dvůr EU – IP / IT", window: "1 měsíc" },
  { id: "journals", label: "Právní časopisy", feed: "journals_feed.xml", archive: "journals_archive.xml",
    kindLabel: "Právní časopisy", window: "1 měsíc" },
];
const BY_ID = Object.fromEntries(COLLECTIONS.map((c) => [c.id, c]));
// Pořadí odpovídá sidebaru – klávesy 1–8 přepínají sbírky v tomto pořadí.
const VIEW_IDS = ["new", "all"]
  .concat(COLLECTIONS.map((c) => c.id))
  .concat(COLLECTIONS.map((c) => "arch-" + c.id));
const ARCHIVE_NOTE = "Archiv se nikdy nemaže · v hlavním seznamu zůstává " +
  COLLECTIONS.map((c) => c.label.split(" · ")[0] + " " + c.window).join(", ");
const TYPE_LABELS = { ruling: "Ruling", referral: "Referral", appeal: "Appeal", pending: "Pending" };

/* ---------- 2. Úložiště ---------- */
const store = {
  get(key, fallback) {
    try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); }
    catch { return fallback; }
  },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode */ } },
};
const K = {
  read: "prf.read", sort: "prf.sort", density: "prf.density", unreadOnly: "prf.unreadOnly",
  tagsOpen: "prf.tagsOpen", wSidebar: "prf.wSidebar", wDetail: "prf.wDetail",
};

/* ---------- Stav ---------- */
let allItems = [];          // normalizované položky (živé + historické archivní)
let byKey = new Map();
let view = "new";
let query = "";
let tagFilter = new Set();
let selectedKey = null;
let currentRows = [];       // položky právě vykresleného seznamu
let newSnapshot = new Set();// členství pohledu „Nové" – drží řádky na místě i po přečtení
let loadErrors = [];
let loaded = false;

let readSet = new Set(store.get(K.read, []));
let sort = store.get(K.sort, { key: "date", dir: -1 });
let unreadOnly = store.get(K.unreadOnly, false);
let density = store.get(K.density, "compact");

/* ---------- 3. Pomocné funkce a ikony ---------- */
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const collapse = (s) => String(s ?? "").replace(/\s+/g, " ").trim();
const txt = (el, sel) => { const c = el.querySelector(sel); return c ? c.textContent.trim() : ""; };
const fmtDate = (d) => (d ? d.toLocaleDateString("cs-CZ") : "");
const isoToCs = (s) => { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s); return m ? `${+m[3]}. ${+m[2]}. ${+m[1]}` : s; };
const plural = (n, one, few, many) => (n === 1 ? one : n >= 2 && n <= 4 ? few : many);
const isUnread = (it) => !it.archived && !readSet.has(it.key);

const ICO = {
  lib: '<svg class="ico" width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" aria-hidden="true"><path d="M2 5.6 8 2l6 3.6M3.4 6.4v6M6.5 6.4v6M9.5 6.4v6M12.6 6.4v6M2.2 13.6h11.6"/></svg>',
  star: '<svg class="ico" width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" aria-hidden="true"><path d="M8 1.9 9.9 5.8l4.3.6-3.1 3 .7 4.3L8 11.7l-3.8 2 .7-4.3-3.1-3 4.3-.6z"/></svg>',
  folder: '<svg class="ico" width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" aria-hidden="true"><path d="M1.8 4.2c0-.6.4-1 1-1h3.1l1.5 1.6h6c.5 0 .9.4.9 1v6.6c0 .6-.4 1-1 1H2.8c-.6 0-1-.4-1-1z"/></svg>',
  box: '<svg class="ico" width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" aria-hidden="true"><rect x="1.6" y="2.4" width="12.8" height="2.8" rx=".6"/><path d="M2.6 5.2v7.2c0 .6.4 1 1 1h8.8c.6 0 1-.4 1-1V5.2M6.3 8h3.4"/></svg>',
  rss: '<svg class="ico" width="11" height="11" viewBox="0 0 16 16" aria-hidden="true"><circle cx="3.6" cy="12.4" r="1.9" fill="currentColor"/><path d="M2 7.3a6.7 6.7 0 0 1 6.7 6.7M2 2.8a11.2 11.2 0 0 1 11.2 11.2" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round"/></svg>',
  open: '<svg class="ico" width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><path d="M6.5 3H3.2c-.7 0-1.2.5-1.2 1.2v8.6c0 .7.5 1.2 1.2 1.2h8.6c.7 0 1.2-.5 1.2-1.2V9.5M9.8 2h4.2v4.2M14 2 7.5 8.5"/></svg>',
  copy: '<svg class="ico" width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true"><rect x="5.4" y="5.4" width="8.2" height="8.2" rx="1.2"/><path d="M10.6 5.4V3.6c0-.7-.5-1.2-1.2-1.2H3.6c-.7 0-1.2.5-1.2 1.2v5.8c0 .7.5 1.2 1.2 1.2h1.8"/></svg>',
  dot: '<svg class="ico" width="12" height="12" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="4" fill="currentColor"/></svg>',
  rows: '<svg class="ico" width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" aria-hidden="true"><path d="M2 4h12M2 8h12M2 12h8"/></svg>',
  check: '<svg class="ico" width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m2.6 8.4 3.2 3.2 7.6-7.6"/></svg>',
};

function docIcon(kind) {
  if (kind === "journals") {
    return '<svg class="ico row-ico k-journals" width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" aria-hidden="true"><rect x="3" y="1.7" width="10" height="12.6" rx="1.2"/><path d="M5.7 1.7v12.6M7.6 5h3.4M7.6 7.4h3.4"/></svg>';
  }
  return '<svg class="ico row-ico k-' + kind + '" width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" aria-hidden="true"><path d="M3.6 1.7h6l2.8 2.8v9.8H3.6z"/><path d="M9.6 1.7v2.8h2.8M5.7 8h4.6M5.7 10.4h4.6"/></svg>';
}

let toastTimer = 0;
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2200);
}

function copyText(text, okMsg) {
  const fallback = () => {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.cssText = "position:fixed;opacity:0";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch { ok = false; }
    ta.remove();
    toast(ok ? okMsg : "Kopírování se nezdařilo");
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(() => toast(okMsg), fallback);
  } else {
    fallback();
  }
}

/* ---------- 4. Načtení a normalizace feedů ---------- */
function normalizeItem(el, col, archived) {
  const it = {
    col: col.id, kind: col.id, archived,
    id: txt(el, "guid") || txt(el, "link"),
    link: txt(el, "link"),
    heslo: txt(el, "ai-tag"),
    summary: txt(el, "ai-summary"),
    fresh: !archived && !!el.querySelector("is-new"),
    typeTag: "", typeName: "", nsCategory: txt(el, "category"),
    taxonomy: [], journal: "", authors: "", form: "", decided: "", announced: "", nsKat: "",
  };
  it.key = it.col + "|" + it.id;

  const rawTitle = txt(el, "title");
  const desc = txt(el, "description");
  const pd = txt(el, "pubDate");
  it.date = pd ? new Date(pd) : null;
  if (it.date && isNaN(it.date)) it.date = null;
  if (!it.summary && desc) it.summary = collapse(desc.split(/\n\s*\n/)[0]);

  if (col.id === "nsoud") {
    it.title = collapse(rawTitle).replace(/\s*\/\s*/g, "/");
    // Metadata jsou v závorce za prázdným řádkem na konci popisu:
    // „(Usnesení 23 Cdo 308/2026, Heslo: …, rozhodnuto 2026-06-30, kategorie E)".
    // Staré archivní záznamy mají tentýž řádek jako celý popis, bez shrnutí.
    let tail = null;
    const m = /\n\s*\n\(([\s\S]*)\)\s*$/.exec(desc);
    if (m) tail = m[1];
    else if (/^(Rozsudek|Usnesení|Rozhodnutí|Stanovisko)\b[^\n]*vyhlášeno/i.test(desc.trim())) {
      tail = desc.trim();
      it.summary = "";
    }
    if (tail) {
      const form = /^(Rozsudek|Usnesení|Rozhodnutí|Stanovisko)/i.exec(tail);
      if (form) it.form = form[1];
      const dec = /rozhodnuto\s+(\d{4}-\d{2}-\d{2})/i.exec(tail);
      if (dec) { it.decided = isoToCs(dec[1]); it.decidedIso = dec[1]; }
      const ann = /vyhlášeno\s+(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})/i.exec(tail);
      if (ann) {
        it.announced = +ann[1] + ". " + +ann[2] + ". " + ann[3];
        it.announcedIso = ann[3] + "-" + ann[2].padStart(2, "0") + "-" + ann[1].padStart(2, "0");
      }
      const kat = /kategorie\s+([A-F])/i.exec(tail);
      if (kat) it.nsKat = kat[1];
    }
  } else if (col.id === "cjeu") {
    const m = /^\[(\w+)\]\s*/.exec(rawTitle);
    if (m) it.typeTag = m[1].toLowerCase();
    it.title = collapse(rawTitle.replace(/^\[\w+\]\s*/, ""));
    const tn = /^Type:\s*(.+)$/m.exec(desc);
    if (tn) it.typeName = tn[1].trim();
    it.taxonomy = desc.split("\n").filter((l) => l.trim().startsWith("- "))
      .map((l) => collapse(l).slice(2).replace(/\s*>\s*/g, " › "));
    const cn = /(C-\d+\/\d+)/.exec(it.title);
    it.caseNo = cn ? cn[1] : "";
    const party = /\(([^)]+)\)\s*$/.exec(it.title);
    it.party = party ? collapse(party[1]) : "";
  } else {
    it.title = collapse(rawTitle.replace(/^\[\w+\]\s*/, ""));
    it.journal = it.id.startsWith("RPT-") ? "Revue pro právo a technologie"
      : it.title.replace(/\s+\d+\/\d{4}$/, "");
    const au = /(?:^|\n)Autor:\s*([^\n]+)/.exec(desc);
    if (au) it.authors = collapse(au[1]);
  }
  return it;
}

function fetchXml(url) {
  return fetch(url).then((r) => {
    if (!r.ok) throw new Error(url + ": HTTP " + r.status);
    return r.text();
  }).then((s) => {
    const doc = new DOMParser().parseFromString(s, "text/xml");
    if (doc.querySelector("parsererror")) throw new Error(url + ": chybné XML");
    return doc;
  });
}

function relTime(d) {
  const mins = Math.round((Date.now() - d.getTime()) / 60000);
  if (mins < 60) return "před " + mins + " " + plural(mins, "minutou", "minutami", "minutami");
  const h = Math.round(mins / 60);
  if (h < 24) return "před " + h + " " + plural(h, "hodinou", "hodinami", "hodinami");
  const days = Math.round(h / 24);
  if (days < 30) return "před " + days + " " + plural(days, "dnem", "dny", "dny");
  return d.toLocaleDateString("cs-CZ");
}

function loadAll() {
  const jobs = COLLECTIONS.flatMap((c) => [
    fetchXml(c.feed).then((d) => ({ c, d, archived: false })),
    fetchXml(c.archive).then((d) => ({ c, d, archived: true })),
  ]);
  return Promise.allSettled(jobs).then((results) => {
    const items = [];
    let newest = null;
    for (const r of results) {
      if (r.status === "rejected") { loadErrors.push(String(r.reason && r.reason.message || r.reason)); continue; }
      const { c, d, archived } = r.value;
      if (!archived) {
        const lbd = txt(d, "channel > lastBuildDate");
        const t = lbd ? new Date(lbd) : null;
        if (t && !isNaN(t) && (!newest || t > newest)) newest = t;
      }
      d.querySelectorAll("item").forEach((el) => items.push(normalizeItem(el, c, archived)));
    }

    // Archiv = jen historické položky; co je právě v živém feedu, se v archivu neopakuje.
    const liveKeys = new Set(items.filter((i) => !i.archived).map((i) => i.key));
    let kept = items.filter((i) => !i.archived || !liveKeys.has(i.key));
    // Starý formát archivu NS měl guid = spisovou značku a žádný odkaz. Když táž věc
    // existuje i v novém formátu (s odkazem), duplicitní záznam skryjeme.
    const linked = new Set(kept.filter((i) => i.col === "nsoud" && i.link).map((i) => i.title));
    kept = kept.filter((i) => !(i.col === "nsoud" && i.archived && !i.link && linked.has(i.title)));

    allItems = kept;
    allItems.forEach((i, idx) => { i.ord = idx; });
    byKey = new Map(allItems.map((i) => [i.key, i]));
    loaded = true;
    pruneRead();

    if (newest) {
      $("#updated").textContent = "Feedy aktualizovány " + relTime(newest) + ".";
      $("#updated").title = newest.toLocaleString("cs-CZ");
    }
  });
}

/* Přečtené držíme jen pro položky, které ještě známe (jinak by seznam rostl donekonečna). */
function pruneRead() {
  const before = readSet.size;
  readSet = new Set([...readSet].filter((k) => byKey.has(k)));
  if (readSet.size !== before) saveRead();
}
function saveRead() { store.set(K.read, [...readSet]); }

/* ---------- 5. Výběr pohledu ---------- */
function baseItems(v) {
  if (v === "new") return allItems.filter((i) => !i.archived && newSnapshot.has(i.key));
  if (v === "all") return allItems.filter((i) => !i.archived);
  if (v.startsWith("arch-")) { const c = v.slice(5); return allItems.filter((i) => i.archived && i.col === c); }
  return allItems.filter((i) => !i.archived && i.col === v);
}

function matchesQuery(i, q) {
  return (i.title + " " + i.heslo + " " + i.summary + " " + i.nsCategory + " " +
    i.taxonomy.join(" ") + " " + i.journal + " " + i.authors).toLowerCase().includes(q);
}

function sortItems(arr) {
  const dir = sort.dir;
  const key = sort.key;
  return arr.slice().sort((a, b) => {
    let d = 0;
    if (key === "date") d = (a.date ? a.date.getTime() : 0) - (b.date ? b.date.getTime() : 0);
    else if (key === "heslo") d = (a.heslo || "￿").localeCompare(b.heslo || "￿", "cs");
    else d = a.title.localeCompare(b.title, "cs", { numeric: true });
    return d * dir || a.ord - b.ord;
  });
}

/** Položky viditelné v seznamu po všech filtrech (bez řazení do skupin). */
function visibleItems() {
  let arr = baseItems(view);
  if (unreadOnly && view !== "new" && !view.startsWith("arch-")) {
    arr = arr.filter((i) => isUnread(i) || i.key === selectedKey);
  }
  if (tagFilter.size) arr = arr.filter((i) => tagFilter.has(i.heslo));
  if (query) { const q = query.toLowerCase(); arr = arr.filter((i) => matchesQuery(i, q)); }
  return sortItems(arr);
}

function setView(v, force) {
  if (!VIEW_IDS.includes(v)) return;
  if (view === v && !force) return;
  view = v;
  if (v === "new") refreshNewSnapshot();
  if (location.hash.slice(1) !== v) { history.replaceState(null, "", "#" + v); }
  render();
  $("#list-scroll").scrollTop = 0;
}

/** Pohled „Nové" si zapamatuje, co v něm bylo při vstupu – přečtené řádky pak
    nemizí zpod kurzoru, jen zšednou. Obnoví se při dalším otevření sbírky. */
function refreshNewSnapshot() {
  newSnapshot = new Set(allItems.filter((i) => isUnread(i)).map((i) => i.key));
}

/* ---------- 6. Sidebar ---------- */
function sideRow(id, icon, label, rssHref) {
  const rss = rssHref
    ? '<a class="rss-link" href="' + esc(rssHref) + '" title="RSS: ' + esc(rssHref) + '" aria-label="RSS feed – ' + esc(label) + '">' + ICO.rss + "</a>"
    : "";
  return '<li><a class="side-item" href="#' + esc(id) + '" data-view="' + esc(id) + '">' + icon +
    '<span class="lbl">' + esc(label) + '</span><span class="cnt" data-cnt="' + esc(id) + '"></span></a>' + rss + "</li>";
}

function renderSidebar() {
  $("#side-smart").innerHTML = sideRow("new", ICO.star, "Nové") + sideRow("all", ICO.lib, "Vše");
  $("#side-live").innerHTML = COLLECTIONS.map((c) => sideRow(c.id, ICO.folder, c.label, c.feed)).join("");
  $("#side-arch").innerHTML = COLLECTIONS.map((c) => sideRow("arch-" + c.id, ICO.box, c.label, c.archive)).join("");
}

function updateCounts() {
  const live = allItems.filter((i) => !i.archived);
  const unreadOf = (arr) => arr.filter(isUnread).length;
  document.querySelectorAll("[data-cnt]").forEach((el) => {
    const v = el.getAttribute("data-cnt");
    const item = el.closest(".side-item");
    let total, unread = 0;
    if (v === "new") { total = unreadOf(live); unread = total; }
    else if (v === "all") { total = live.length; unread = unreadOf(live); }
    else if (v.startsWith("arch-")) { total = allItems.filter((i) => i.archived && i.col === v.slice(5)).length; }
    else { const a = live.filter((i) => i.col === v); total = a.length; unread = unreadOf(a); }
    el.textContent = unread ? String(unread) : total ? String(total) : "";
    el.classList.toggle("unread", unread > 0);
    el.title = unread ? unread + " nepřečtených z " + total : "";
    item.classList.toggle("has-unread", unread > 0);
  });
}

function renderTags() {
  const pane = $("#tagpane");
  let pool = baseItems(view);
  if (query) { const q = query.toLowerCase(); pool = pool.filter((i) => matchesQuery(i, q)); }

  const counts = new Map();
  pool.forEach((i) => { if (i.heslo) counts.set(i.heslo, (counts.get(i.heslo) || 0) + 1); });
  // Hesla vybraná ve filtru necháme v seznamu i tehdy, když je zrovna nic nesplňuje.
  tagFilter.forEach((t) => { if (!counts.has(t)) counts.set(t, 0); });

  const tags = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], "cs"));
  pane.hidden = tags.length === 0;
  $("#tagpane-cnt").textContent = tagFilter.size ? tagFilter.size + " z " + tags.length : String(tags.length);
  $("#tags").innerHTML = tags.map(([t, n]) =>
    '<button type="button" class="tagbtn" data-tag="' + esc(t) + '" aria-pressed="' +
    (tagFilter.has(t) ? "true" : "false") + '" title="' + esc(t) + '">' + esc(t) +
    ' <span class="n">' + n + "</span></button>").join("");
}

/* ---------- 7. Seznam položek ---------- */
const DAY = 86400000;
// Na úzkém okně se sloupec „Heslo" nevykresluje vůbec – skrytí přes CSS by mu
// v table-layout: fixed nechalo šířku a zbylé sloupce by se nerozšířily.
const mqNarrow = window.matchMedia("(max-width: 720px)");
function groupLabel(d) {
  if (!d) return "Bez data";
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const day = new Date(d); day.setHours(0, 0, 0, 0);
  const diff = Math.round((today - day) / DAY);
  if (diff === 0) return "Dnes";
  if (diff === 1) return "Včera";
  if (diff > 1 && diff < 7) return d.toLocaleDateString("cs-CZ", { weekday: "long", day: "numeric", month: "numeric" });
  return d.toLocaleDateString("cs-CZ", { day: "numeric", month: "long", year: "numeric" });
}

function rowHtml(it) {
  const unread = isUnread(it);
  let c1 = '<span class="row-dot' + (unread ? "" : " hidden") + '" aria-hidden="true"></span>' + docIcon(it.kind);
  if (it.typeTag) {
    c1 += '<span class="tdot tdot-' + esc(it.typeTag) + '" title="' +
      esc(TYPE_LABELS[it.typeTag] || it.typeTag) + '"></span>';
  }
  c1 += '<span class="rt">' + esc(it.title) + "</span>";
  if (it.fresh) c1 += '<span class="fresh" title="Do feedu přibylo dnes">nové</span>';
  if (it.summary) c1 += '<div class="prev">' + esc(it.summary) + "</div>";

  const heslo = mqNarrow.matches ? ""
    : '<td class="c-heslo" role="gridcell">' + (it.heslo ? '<span class="chip">' + esc(it.heslo) + "</span>" : "") + "</td>";
  return '<tr class="item' + (unread ? " unread" : "") + (it.key === selectedKey ? " sel" : "") +
    '" data-id="' + esc(it.key) + '" tabindex="-1" role="row" aria-selected="' +
    (it.key === selectedKey ? "true" : "false") + '">' +
    '<td role="gridcell">' + c1 + "</td>" + heslo +
    '<td class="c-date" role="gridcell">' + esc(fmtDate(it.date)) + "</td></tr>";
}

function headHtml() {
  const col = (key, label, cls) => {
    const on = sort.key === key;
    const state = on ? (sort.dir === 1 ? "ascending" : "descending") : "none";
    return '<th' + (cls ? ' class="' + cls + '"' : "") + ' aria-sort="' + state + '" scope="col">' +
      '<button type="button" data-sort="' + key + '">' + label +
      '<span class="sort" aria-hidden="true">' + (on ? (sort.dir === 1 ? "▲" : "▼") : "▼") + "</span></button></th>";
  };
  return "<thead><tr>" + col("title", "Název") +
    (mqNarrow.matches ? "" : col("heslo", "Heslo", "c-heslo")) +
    col("date", "Datum", "c-date") + "</tr></thead>";
}

function emptyState() {
  if (!loaded) return '<div class="skeleton" aria-hidden="true">' + "<i></i>".repeat(12) + "</div>";
  if (query || tagFilter.size) {
    return '<div class="list-msg"><strong>Nic nenalezeno</strong>Zkuste jiný dotaz nebo zrušte filtr hesel.' +
      '<div><button type="button" class="tbtn" data-act="clear-filters">Zrušit filtry</button></div></div>';
  }
  if (view === "new") {
    return '<div class="list-msg"><strong>Vše přečteno</strong>Nic nového od poslední návštěvy.' +
      '<div><button type="button" class="tbtn" data-act="goto-all">Zobrazit všechny položky</button></div></div>';
  }
  if (unreadOnly) {
    return '<div class="list-msg"><strong>Žádné nepřečtené</strong>Ve filtru je zapnuto „jen nepřečtené".' +
      '<div><button type="button" class="tbtn" data-act="show-all-items">Zobrazit i přečtené</button></div></div>';
  }
  if (loadErrors.length) return '<div class="list-msg"><strong>Feed se nepodařilo načíst</strong>' + esc(loadErrors[0]) + "</div>";
  return '<div class="list-msg">Žádné položky.</div>';
}

function render() {
  currentRows = loaded ? visibleItems() : [];
  const scroll = $("#list-scroll");

  if (!currentRows.length) {
    scroll.innerHTML = emptyState();
  } else {
    const grouped = sort.key === "date" && sort.dir === -1;
    let body = "", lastGroup = null;
    for (const it of currentRows) {
      if (grouped) {
        const g = groupLabel(it.date);
        if (g !== lastGroup) {
          lastGroup = g;
          body += '<tr class="grp" role="row"><td colspan="' + (mqNarrow.matches ? 2 : 3) +
            '" role="gridcell">' + esc(g) + "</td></tr>";
        }
      }
      body += rowHtml(it);
    }
    // Bez <colgroup> – šířky drží třídy na <th> (table-layout: fixed), takže
    // skrytí sloupce na mobilu opravdu uvolní místo zbylým sloupcům.
    scroll.innerHTML = '<table role="grid" aria-label="Seznam položek">' +
      headHtml() + "<tbody>" + body + "</tbody></table>";
  }

  document.querySelectorAll(".side-item").forEach((el) => {
    const on = el.getAttribute("data-view") === view;
    el.classList.toggle("active", on);
    el.setAttribute("aria-current", on ? "true" : "false");
  });

  updateCounts();
  renderTags();
  renderStatus();

  // Obnova výběru; když vybraná položka ze seznamu zmizela, detail vyprázdníme.
  if (!selectedKey) { /* nic vybraného */ }
  else if (currentRows.some((i) => i.key === selectedKey)) focusRow(selectedKey, false);
  else clearSelection();
  const first = scroll.querySelector("tr.item");
  if (first && !scroll.querySelector('tr.item[tabindex="0"]')) first.tabIndex = 0;
}

function renderStatus() {
  const n = currentRows.length;
  const unread = currentRows.filter(isUnread).length;
  let s = n + " " + plural(n, "položka", "položky", "položek");
  if (query || tagFilter.size || (unreadOnly && view !== "new")) s += " (filtrováno)";
  if (unread) s += " · " + unread + " " + plural(unread, "nepřečtená", "nepřečtené", "nepřečtených");
  $("#status-count").textContent = s;
  $("#export-ris").disabled = n === 0;
  $("#export-ris").textContent = "Export RIS (" + n + ")";
  $("#mark-read").disabled = unread === 0;
  $("#status-note").textContent = view.startsWith("arch-") ? ARCHIVE_NOTE
    : loadErrors.length ? "Nenačteno: " + loadErrors.join(", ") : "";
}

/* ---------- Výběr a čtení ---------- */
function focusRow(key, scrollIntoView) {
  document.querySelectorAll("tbody tr.item").forEach((tr) => {
    const on = tr.getAttribute("data-id") === key;
    tr.classList.toggle("sel", on);
    tr.setAttribute("aria-selected", on ? "true" : "false");
    tr.tabIndex = on ? 0 : -1;
    if (on && scrollIntoView !== false) tr.scrollIntoView({ block: "nearest" });
  });
}

function clearSelection() {
  selectedKey = null;
  const inner = $("#detail-inner");
  inner.className = "detail-empty";
  inner.textContent = "Vyberte položku ze seznamu";
  document.body.classList.remove("detail-open");
}

function selectItem(key, openPanel) {
  const it = byKey.get(key);
  if (!it) return;
  selectedKey = key;
  focusRow(key, true);
  if (isUnread(it)) {
    readSet.add(key);
    saveRead();
    const tr = document.querySelector('tbody tr[data-id="' + CSS.escape(key) + '"]');
    if (tr) {
      tr.classList.remove("unread");
      const dot = tr.querySelector(".row-dot");
      if (dot) dot.classList.add("hidden");
    }
    updateCounts();
    renderStatus();
  }
  renderDetail(it);
  if (openPanel) document.body.classList.add("detail-open");
}

function setRead(key, read) {
  if (read) readSet.add(key); else readSet.delete(key);
  saveRead();
  render();
  if (selectedKey === key) { const it = byKey.get(key); if (it) renderDetail(it); }
}

function markListedRead() {
  const unread = currentRows.filter(isUnread);
  if (!unread.length) return;
  unread.forEach((i) => readSet.add(i.key));
  saveRead();
  render();
  toast("Označeno " + unread.length + " " + plural(unread.length, "položka", "položky", "položek") + " jako přečtené");
}

/* ---------- 8. Detail, citace a RIS ---------- */
const COURT_FORM = { Judgment: "Rozsudek", Order: "Usnesení", Opinion: "Stanovisko" };

function citationOf(it) {
  const date = it.decided || it.announced || fmtDate(it.date);
  if (it.kind === "nsoud") {
    return (it.form || "Rozhodnutí") + " Nejvyššího soudu ze dne " + date +
      ", sp. zn. " + it.title + (it.nsKat ? " (kategorie " + it.nsKat + ")" : "") + ".";
  }
  if (it.kind === "cjeu") {
    const who = it.typeTag === "referral"
      ? "Žádost o rozhodnutí o předběžné otázce ze dne " + fmtDate(it.date)
      : (COURT_FORM[it.typeName] || "Rozhodnutí") + " Soudního dvora ze dne " + fmtDate(it.date);
    return who + (it.party ? ", " + it.party : "") + (it.caseNo ? ", věc " + it.caseNo : "") + ".";
  }
  const authors = it.authors ? it.authors.toUpperCase() + ". " : "";
  const title = titleWithoutAuthors(it);
  return authors + title + ". " + (it.journal ? it.journal + " " : "") + "[online]. " +
    (it.date ? it.date.getFullYear() + ". " : "") + (it.link ? "Dostupné z: " + it.link : "");
}

function titleWithoutAuthors(it) {
  if (it.authors && it.title.endsWith("– " + it.authors)) {
    return it.title.slice(0, -("– " + it.authors).length).trim();
  }
  return it.title;
}

/** „Martin Erlebach" → „Erlebach, Martin" (RIS očekává příjmení první). */
function risAuthors(authors) {
  if (!authors) return [];
  return authors.split(/\s*,\s*/).map((full) => {
    const parts = full.trim().split(/\s+/);
    if (parts.length < 2) return full.trim();
    return parts[parts.length - 1] + ", " + parts.slice(0, -1).join(" ");
  }).filter(Boolean);
}

function risDate(it) {
  const iso = it.decidedIso || it.announcedIso;
  if (iso) return { da: iso.replace(/-/g, "/") + "/", py: iso.slice(0, 4) };
  if (it.date) {
    const p = (n) => String(n).padStart(2, "0");
    return {
      da: it.date.getFullYear() + "/" + p(it.date.getMonth() + 1) + "/" + p(it.date.getDate()) + "/",
      py: String(it.date.getFullYear()),
    };
  }
  return { da: "", py: "" };
}

/** RIS záznam ve tvaru, který používá zdejší zoterovská knihovna
    (vlajka + zkratka věci v TI, shrnutí v AB, spisová značka v SV). */
function risOf(it) {
  const L = [];
  const add = (tag, value) => { if (value) L.push(tag.padEnd(2) + "  - " + value); };
  const { da, py } = risDate(it);

  if (it.kind === "journals") {
    add("TY", "JOUR");
    add("TI", titleWithoutAuthors(it));
    risAuthors(it.authors).forEach((a) => add("AU", a));
    add("T2", it.journal);
  } else {
    const flag = it.kind === "cjeu" ? "🇪🇺" : "🇨🇿";
    const nick = it.kind === "cjeu" ? (it.party || it.caseNo || it.title) : it.title;
    add("TY", "CASE");
    add("TI", flag + " " + nick + (it.heslo ? " - " + it.heslo : ""));
    add("PB", it.kind === "cjeu" ? "CJEU" : "Nejvyšší soud");
    add("SV", it.kind === "cjeu" ? (it.caseNo || it.title) : it.title);
  }
  add("AB", it.summary);
  add("DA", da);
  add("PY", py);
  add("LA", it.kind === "cjeu" ? "en" : "cs");
  if (it.heslo) add("KW", it.heslo);
  add("UR", it.link);
  L.push("ER  - ");
  return L.join("\n");
}

function exportRis() {
  if (!currentRows.length) return;
  const blob = new Blob([currentRows.map(risOf).join("\n") + "\n"], { type: "application/x-research-info-systems" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "pravni-rss-" + view + ".ris";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  toast("Staženo " + currentRows.length + " " + plural(currentRows.length, "záznam", "záznamy", "záznamů") + " RIS");
}

function metaRow(dt, dd) { return dd ? "<dt>" + esc(dt) + "</dt><dd>" + esc(dd) + "</dd>" : ""; }

function renderDetail(it) {
  const col = BY_ID[it.col];
  let h = '<div class="d-kind">' + esc(col.kindLabel) + (it.archived ? " · archiv" : "") + "</div>";
  h += "<h2>" + esc(titleWithoutAuthors(it)) + "</h2>";

  let badges = "";
  if (it.typeTag) {
    badges += '<span class="tag tag-' + esc(it.typeTag) + '">' + esc(TYPE_LABELS[it.typeTag] || it.typeTag) + "</span>";
  }
  if (it.heslo) badges += '<span class="chip">' + esc(it.heslo) + "</span>";
  if (it.fresh) badges += '<span class="tag tag-fresh">přibylo dnes</span>';
  if (badges) h += '<div class="d-badges">' + badges + "</div>";

  let meta = "";
  if (it.kind === "nsoud") {
    meta += metaRow("Forma", it.form);
    meta += metaRow("Rozhodnuto", it.decided);
    meta += metaRow("Vyhlášeno", it.announced);
    meta += metaRow("Kategorie", it.nsKat);
    meta += metaRow("Zveřejněno", fmtDate(it.date));
  } else if (it.kind === "cjeu") {
    meta += metaRow("Věc", it.caseNo);
    meta += metaRow("Typ dokumentu", it.typeName);
    meta += metaRow("Datum", fmtDate(it.date));
  } else {
    meta += metaRow("Časopis", it.journal);
    meta += metaRow("Autoři", it.authors);
    meta += metaRow("Datum", fmtDate(it.date));
  }
  if (meta) h += '<dl class="meta">' + meta + "</dl>";

  if (it.summary) h += '<div class="d-sec">Shrnutí</div><p class="d-summary">' + esc(it.summary) + "</p>";
  if (it.nsCategory) h += '<div class="d-sec">Hesla NS</div><p class="d-summary">' + esc(it.nsCategory) + "</p>";
  if (it.taxonomy.length) {
    h += '<div class="d-sec">Klasifikace CURIA</div><ul class="d-tax">' +
      it.taxonomy.map((t) => "<li>" + esc(t) + "</li>").join("") + "</ul>";
  }

  h += '<div class="d-actions">';
  if (it.link) {
    const label = it.kind === "cjeu" ? "Otevřít na CURIA"
      : /\.pdf(\?|$)/i.test(it.link) ? "Otevřít PDF"
      : it.kind === "journals" ? "Otevřít článek" : "Otevřít odkaz";
    h += '<a class="d-btn primary" href="' + esc(it.link) + '" target="_blank" rel="noopener">' + ICO.open + esc(label) + "</a>";
  }
  h += '<button type="button" class="d-btn" data-act="cite">' + ICO.copy + "Citace</button>";
  h += '<button type="button" class="d-btn" data-act="ris">' + ICO.copy + "RIS</button>";
  if (!it.archived) {
    h += '<button type="button" class="d-btn" data-act="toggle-read">' +
      (isUnread(it) ? ICO.check + "Označit jako přečtené" : ICO.dot + "Označit jako nepřečtené") + "</button>";
  }
  h += "</div>";

  const inner = $("#detail-inner");
  inner.className = "";
  inner.innerHTML = h;
}

/* ---------- 9. Události a klávesnice ---------- */
function moveSelection(delta) {
  if (!currentRows.length) return;
  let idx = currentRows.findIndex((i) => i.key === selectedKey);
  idx = idx < 0 ? (delta > 0 ? 0 : currentRows.length - 1)
    : Math.min(currentRows.length - 1, Math.max(0, idx + delta));
  const wide = window.matchMedia("(min-width: 1101px)").matches;
  selectItem(currentRows[idx].key, wide);
  const tr = document.querySelector('tbody tr[data-id="' + CSS.escape(currentRows[idx].key) + '"]');
  if (tr) tr.focus({ preventScroll: true });
}

function openSelected() {
  const it = selectedKey && byKey.get(selectedKey);
  if (it && it.link) window.open(it.link, "_blank", "noopener");
}

function clearFilters() {
  query = ""; $("#search").value = "";
  tagFilter.clear();
  render();
}

function setDensity(next) {
  density = next;
  store.set(K.density, density);
  document.body.classList.toggle("density-preview", density === "preview");
  const b = $("#toggle-density");
  b.setAttribute("aria-pressed", density === "preview" ? "true" : "false");
  b.title = density === "preview" ? "Skrýt náhled shrnutí (v)" : "Zobrazit náhled shrnutí (v)";
}

function setUnreadOnly(next) {
  unreadOnly = next;
  store.set(K.unreadOnly, unreadOnly);
  $("#toggle-unread").setAttribute("aria-pressed", unreadOnly ? "true" : "false");
  render();
}

function setSort(key) {
  if (sort.key === key) sort.dir = -sort.dir;
  else sort = { key, dir: key === "date" ? -1 : 1 };
  store.set(K.sort, sort);
  render();
}

document.addEventListener("click", (e) => {
  const side = e.target.closest(".side-item");
  if (side) { e.preventDefault(); setView(side.getAttribute("data-view"), true); return; }

  const tag = e.target.closest(".tagbtn");
  if (tag) {
    const t = tag.getAttribute("data-tag");
    if (tagFilter.has(t)) tagFilter.delete(t); else tagFilter.add(t);
    render();
    return;
  }

  const sortBtn = e.target.closest("[data-sort]");
  if (sortBtn) { setSort(sortBtn.getAttribute("data-sort")); return; }

  const row = e.target.closest("tbody tr.item");
  if (row) { selectItem(row.getAttribute("data-id"), true); return; }

  const act = e.target.closest("[data-act]");
  if (act) {
    const it = selectedKey && byKey.get(selectedKey);
    switch (act.getAttribute("data-act")) {
      case "cite": if (it) copyText(citationOf(it), "Citace zkopírována"); break;
      case "ris": if (it) copyText(risOf(it), "RIS zkopírován – v Zoteru File → Import from Clipboard"); break;
      case "toggle-read": if (it) setRead(it.key, isUnread(it)); break;
      case "clear-filters": clearFilters(); break;
      case "goto-all": setView("all", true); break;
      case "show-all-items": setUnreadOnly(false); break;
    }
    return;
  }

  if (e.target.closest("#backdrop") || e.target.closest("#detail-close")) {
    document.body.classList.remove("detail-open");
  }
});

document.addEventListener("dblclick", (e) => {
  const row = e.target.closest("tbody tr.item");
  if (!row) return;
  const it = byKey.get(row.getAttribute("data-id"));
  if (it && it.link) window.open(it.link, "_blank", "noopener");
});

$("#search").addEventListener("input", (e) => { query = e.target.value.trim(); render(); });
$("#toggle-unread").addEventListener("click", () => setUnreadOnly(!unreadOnly));
$("#toggle-density").addEventListener("click", () => setDensity(density === "preview" ? "compact" : "preview"));
$("#mark-read").addEventListener("click", markListedRead);
$("#export-ris").addEventListener("click", exportRis);
$("#help-open").addEventListener("click", () => $("#help").showModal());
$("#help-close").addEventListener("click", () => $("#help").close());

$("#tagpane-head").addEventListener("click", () => {
  const pane = $("#tagpane");
  const open = pane.classList.toggle("collapsed") === false;
  $("#tagpane-head").setAttribute("aria-expanded", open ? "true" : "false");
  store.set(K.tagsOpen, open);
});

document.addEventListener("keydown", (e) => {
  const dlg = $("#help");
  if (e.key === "Escape") {
    if (dlg.open) return;                       // <dialog> si Esc obslouží sám
    if (document.activeElement === $("#search") && $("#search").value) { clearFilters(); return; }
    if (document.body.classList.contains("detail-open")) { document.body.classList.remove("detail-open"); return; }
    return;
  }
  if (dlg.open) return;
  if (e.target.matches("input, textarea") || e.metaKey || e.ctrlKey || e.altKey) return;

  switch (e.key) {
    case "ArrowDown": case "j": e.preventDefault(); moveSelection(1); break;
    case "ArrowUp": case "k": e.preventDefault(); moveSelection(-1); break;
    case "Enter": case "o": if (selectedKey) { e.preventDefault(); openSelected(); } break;
    case "u": {
      const it = selectedKey && byKey.get(selectedKey);
      if (it && !it.archived) { e.preventDefault(); setRead(it.key, isUnread(it)); }
      break;
    }
    case "v": e.preventDefault(); setDensity(density === "preview" ? "compact" : "preview"); break;
    case "e": e.preventDefault(); setUnreadOnly(!unreadOnly); break;
    case "A": e.preventDefault(); markListedRead(); break;
    case "/": case "f": e.preventDefault(); $("#search").focus(); $("#search").select(); break;
    case "?": e.preventDefault(); dlg.showModal(); break;
    default:
      if (/^[1-8]$/.test(e.key)) { e.preventDefault(); setView(VIEW_IDS[+e.key - 1], true); }
  }
});

window.addEventListener("hashchange", () => {
  const v = location.hash.slice(1);
  if (v && v !== view) setView(v, true);
});

// Přechod mezi širokým a úzkým rozvržením mění skladbu sloupců – překreslíme.
mqNarrow.addEventListener("change", () => render());

/* ---------- 10. Dělítka panelů ---------- */
function initSplitter(el, cssVar, storageKey, min, max, fromRight) {
  const apply = (px) => document.documentElement.style.setProperty(cssVar, px + "px");
  const saved = store.get(storageKey, null);
  if (saved) apply(saved);

  el.addEventListener("pointerdown", (e) => {
    if (!window.matchMedia("(min-width: 1101px)").matches) return;
    e.preventDefault();
    el.setPointerCapture(e.pointerId);
    el.classList.add("dragging");
    document.body.classList.add("resizing");

    const move = (ev) => {
      const px = Math.round(fromRight ? window.innerWidth - ev.clientX : ev.clientX);
      apply(Math.max(min, Math.min(max, px)));
    };
    const up = () => {
      el.classList.remove("dragging");
      document.body.classList.remove("resizing");
      el.removeEventListener("pointermove", move);
      el.removeEventListener("pointerup", up);
      store.set(storageKey, parseInt(getComputedStyle(document.documentElement).getPropertyValue(cssVar), 10));
    };
    el.addEventListener("pointermove", move);
    el.addEventListener("pointerup", up);
  });
}

/* ---------- 11. Start ---------- */
renderSidebar();
setDensity(density);
$("#toggle-unread").setAttribute("aria-pressed", unreadOnly ? "true" : "false");
if (store.get(K.tagsOpen, true) === false) {
  $("#tagpane").classList.add("collapsed");
  $("#tagpane-head").setAttribute("aria-expanded", "false");
}
initSplitter($("#split-sidebar"), "--w-sidebar", K.wSidebar, 170, 420, false);
initSplitter($("#split-detail"), "--w-detail", K.wDetail, 260, 620, true);

// Výchozí pohled je „Nové" – kvůli tomu se sem chodí. Odkaz s #kotvou má přednost.
const initial = location.hash.slice(1);
view = VIEW_IDS.includes(initial) ? initial : "new";
render();

loadAll().then(() => {
  if (view === "new") refreshNewSnapshot();
  render();
  const first = document.querySelector("tbody tr.item");
  if (first) first.tabIndex = 0;
});
