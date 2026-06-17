#!/usr/bin/env python3
"""Sdílené utility pro RSS scrapery: sledování prvního výskytu položek.

Každý feed si drží JSON {guid: ISO datum prvního výskytu}. Podle něj:
  - ponecháme jen položky s prvním výskytem do `weeks` týdnů zpět,
  - označíme položky poprvé viděné dnes (item["is_new"] = True).

Tím je doba zobrazení stabilní (nezávisí na tom, když zdroj přepíše datum)
a u všech feedů jednotná.
"""

import json
import os
from datetime import datetime, timedelta, timezone


def load_seen(state_file):
    """Načte {guid: iso_datum_prvniho_vyskytu}."""
    if not os.path.exists(state_file):
        return {}
    with open(state_file, "r", encoding="utf-8") as f:
        return json.load(f)


def save_seen(state_file, seen, prune_days=90):
    """Uloží stav, vyhodí záznamy starší než prune_days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=prune_days)
    pruned = {}
    for guid, ts in seen.items():
        try:
            if datetime.fromisoformat(ts) >= cutoff:
                pruned[guid] = ts
        except (ValueError, TypeError):
            continue
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(pruned, f, ensure_ascii=False, indent=2)


def filter_by_first_seen(items, guid_of, state_file, weeks=2):
    """Ponechá jen položky s prvním výskytem do `weeks` týdnů zpět.

    Každé ponechané položce nastaví item["is_new"] = True, pokud byla
    poprvé viděna dnes. Stav prvního výskytu zároveň uloží.

    items    – seznam dict položek
    guid_of  – funkce item -> stabilní identifikátor (str)
    """
    seen = load_seen(state_file)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(weeks=weeks)
    today = now.date()

    kept = []
    for item in items:
        guid = guid_of(item)
        if not guid:
            continue
        if guid not in seen:
            seen[guid] = now.isoformat()
        first_seen = datetime.fromisoformat(seen[guid])
        if first_seen >= cutoff:
            item["is_new"] = (first_seen.date() == today)
            kept.append(item)

    save_seen(state_file, seen)
    return kept
