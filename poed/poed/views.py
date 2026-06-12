"""Pure view-model builders: brain JSON results -> panel data. No GTK."""
# All listing/offer reads are defensive: the live trade API omits fields freely (a missing "ign" crashed the panel once).

import math
import re


def _trim(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else str(n)


def _num(x) -> str | None:
    """Return _trim(x) when x is int or float, else None."""
    return _trim(x) if isinstance(x, (int, float)) else None


def _stat_label(text: str, value: str | None) -> str:
    """Human-readable stat label: substitute the rolled value into the template.

    With a value, every `#` placeholder in the template becomes the value string
    ("# to Dexterity" + "30" -> "30 to Dexterity"). Without a value, drop the `#`
    placeholders and collapse the leftover whitespace ("# to maximum Life" ->
    "to maximum Life").
    """
    if value is not None:
        return text.replace("#", value)
    return re.sub(r"\s+", " ", text.replace("#", "")).strip()


def _prop_label(text: str, value: str | None) -> str:
    """Prop label: props carry no `#` template, so append the value to the text
    ("Item Level" + "80" -> "Item Level 80"). Valueless props keep just the text.
    """
    return f"{text} {value}" if value is not None else text


_GROUPS = [  # render order; label shown only when group non-empty
    ("rune", "Runes"),
    ("implicit", "Implicits"),
    ("prefix", "Prefixes"),
    ("suffix", "Suffixes"),
    ("explicit", "Mods"),  # plain (non-advanced) copy lands here untyped
]


def item_card(item: dict | None) -> dict | None:
    if not item:
        return None
    mods = item.get("mods", {})
    groups = [(label, mods[key]) for key, label in _GROUPS if mods.get(key)]
    return {
        "name": item.get("name", ""),
        "base": item.get("baseType", ""),
        "rarity": item.get("rarity", ""),
        "icon": item.get("iconPath"),
        "props": item.get("props", []),
        "groups": groups,
    }


def _dominant_currency(listings: list[dict]) -> str:
    counts: dict[str, int] = {}
    for l in listings:
        c = l.get("priceCurrency", "")
        counts[c] = counts.get(c, 0) + 1
    if not counts:
        return ""
    return max(counts, key=counts.get)


def unique_rows(matches: list[dict], min_exalted: float) -> list[dict]:
    """Unique-scan matches -> panel rows, sorted by price descending.
    `good` flags rows at/above the highlight threshold; shared-art groups
    (ambiguous) show the group max price with a '?' suffix."""
    rows = []
    for m in sorted(matches, key=lambda m: -(m.get("price") or 0)):
        price = m.get("price") or 0
        shown = _trim(round(price)) if price >= 10 else _trim(price)
        trend = m.get("trend")
        rows.append({
            "name": m.get("name", ""),
            "price": shown + ("?" if m.get("ambiguous") else ""),
            "quantity": m.get("quantity", 0),
            "kind": m.get("kind", ""),
            "good": price >= min_exalted,
            # >=10% move over the snapshot window earns an arrow.
            "trend": "↗" if trend is not None and trend >= 0.10
                     else "↘" if trend is not None and trend <= -0.10
                     else "",
        })
    return rows


def price_summary(result: dict) -> dict:
    listings = result.get("listings", [])
    out = {"count": f"{result.get('total', 0)} listings",
           "min": None, "currency": "", "icon": None}
    if not listings:
        return out
    cur = _dominant_currency(listings)
    dom = [l for l in listings if l.get("priceCurrency") == cur]
    out["min"] = _trim(min(l.get("priceAmount", 0) for l in dom))
    out["currency"] = cur
    out["icon"] = dom[0].get("currencyIconPath")
    return out


def price_rows(result: dict) -> list[dict]:
    rows = []
    for l in result.get("listings", []):
        amount = _trim(l.get("priceAmount", 0))
        rows.append({
            "amount": amount,
            "icon": l.get("currencyIconPath"),  # live API may omit; None -> text fallback
            "price": f"{amount} {l.get('priceCurrency', '')}",
            "seller": l.get("accountName", ""),
            "ign": l.get("ign", ""),
            "age": l.get("relativeDate", ""),
            "status": l.get("accountStatus", "offline"),
            "mine": l.get("isMine", False),
            "display_item": l.get("displayItem"),  # hover card source; may be None
        })
    return rows


_DISPLAY_GROUPS = [  # trade API categories; prefix/suffix split is not available for listings
    ("enchantMods", "Enchants"),
    ("runeMods", "Runes"),
    ("implicitMods", "Implicits"),
    ("fracturedMods", "Fractured"),
    ("explicitMods", "Mods"),
    ("desecratedMods", "Desecrated"),
]


def display_item_card(display_item: dict) -> dict:
    groups = []
    for key, label in _DISPLAY_GROUPS:
        lines = [
            {"text": f"{l.get('text', '')}{l.get('value', '')}"}
            for l in (display_item.get(key) or [])
        ]
        if lines:
            groups.append((label, lines))
    title = display_item.get("title") or []
    props = [
        {"text": l.get("text", ""), "value": l.get("value", "")}
        for l in (display_item.get("itemProps") or [])
        if l.get("text")
    ]
    return {
        "name": title[0] if title else "item",
        "base": title[1] if len(title) >= 2 else "",
        "icon": display_item.get("iconPath"),  # brain-cached local path; may be None
        "props": props,
        "groups": groups,
    }


# Map from FilterTag string values (brain/vendor/ee2/src/web/price-check/filters/interfaces.ts)
# to display-group label. Explicit-influencer sub-tags all bucket into "Mods".
# Unknown tags also fall through to "Mods".
_STAT_TAG_LABEL: dict[str, str] = {
    "pseudo": "Pseudo",
    "explicit": "Mods",
    "explicit-shaper": "Mods",
    "explicit-elder": "Mods",
    "explicit-crusader": "Mods",
    "explicit-hunter": "Mods",
    "explicit-redeemer": "Mods",
    "explicit-warlord": "Mods",
    "explicit-delve": "Mods",
    "explicit-veiled": "Mods",
    "explicit-incursion": "Mods",
    "implicit": "Implicits",
    "enchant": "Enchants",
    "rune": "Runes",
    "added-rune": "Runes",
    "crafted": "Mods",
    "scourge": "Mods",
    "fractured": "Mods",
    "corrupted": "Mods",
    "synthesised": "Mods",
    "eldritch": "Mods",
    "variant": "Mods",
    "property": "Mods",
    "desecrated": "Mods",
    "skill": "Mods",
    "mutated": "Mods",
}

# Canonical render order for stat groups; buckets absent in result are dropped.
_STAT_GROUP_ORDER = ["Pseudo", "Implicits", "Mods", "Enchants", "Runes"]


def stat_rows(result: dict) -> list[dict]:
    """Searched-stat list (brain's kind:"price" `stats`) -> render rows.

    id (int index) and tag pass through; value/min go through _trim when numeric
    else None (template `#` placeholders in text are left as-is). enabled is a bool.
    Iteration 4 attaches click-to-toggle/requery keyed on `id`.
    """
    rows = []
    for s in result.get("stats", []):
        text = s.get("text", "")
        val = _num(s.get("value"))
        rows.append({
            "id": s.get("id", ""),
            "text": text,
            "label": _stat_label(text, val),
            "value": val,
            "min": _num(s.get("min")),
            "enabled": bool(s.get("enabled")),
            "tag": s.get("tag", ""),
        })
    return rows


def stat_groups(result: dict) -> list[tuple[str, list[dict]]]:
    """Group stat_rows by filter tag into ordered (label, rows) pairs.

    Order: Pseudo, Mods, Implicits, Enchants, Runes, then any label not in the
    canonical order appended at the end. Same-label buckets are merged (e.g.
    "rune" + "added-rune" both map to "Runes"). Empty buckets are dropped.
    Unknown tags fall back to "Mods".
    "property"-tagged stats are excluded here — they belong to prop_rows.
    """
    # Accumulate rows per label, preserving first-seen insertion order for extras.
    buckets: dict[str, list[dict]] = {}
    for row in stat_rows(result):
        if row["tag"] == "property":
            continue  # property stats live in prop_rows, not here
        label = _STAT_TAG_LABEL.get(row["tag"], "Mods")
        buckets.setdefault(label, []).append(row)

    # Build ordered result: canonical order first, then any extra labels.
    seen: set[str] = set()
    ordered: list[tuple[str, list[dict]]] = []
    for label in _STAT_GROUP_ORDER:
        if label in buckets:
            ordered.append((label, buckets[label]))
            seen.add(label)
    for label, rows in buckets.items():
        if label not in seen:
            ordered.append((label, rows))
    return ordered


def _fmt_rate(x: float) -> str:
    """Adaptive rate string (ninja-style "1 item = X have").

    x >= 1 -> 1 decimal, trailing-zero trimmed (81.4 -> "81.4", 182.0 -> "182").
    0 < x < 1 -> 2 significant digits ("0.41", "0.0055"); never scientific
                 notation (expand any exponent form to plain decimal).
    x <= 0 -> "0".
    """
    if x is None or x <= 0:
        return "0"
    if x >= 1:
        return _trim(round(x, 1))
    s = f"{x:.2g}"
    if "e" in s or "E" in s:
        # tiny value: %g went scientific. Expand to a plain decimal that keeps
        # 2 significant digits. Decimal places = leading zeros after the point
        # + the 2 sig figures.
        places = -int(math.floor(math.log10(x))) + 1
        s = f"{x:.{places}f}".rstrip("0").rstrip(".")
    return s


def currency_view(result: dict) -> dict:
    item_icon = result.get("iconPath")
    rows = []
    for r in result.get("rates", []):
        raw = r.get("rawUnit", 0) or 0
        # Fixed direction: "1 <item> = X <have>". X = rawUnit (have per 1 item).
        # Sub-1 rates carry a grey inverse hint "1 <have> = Y <item>".
        inverse = _trim(round(1 / raw, 1)) if 0 < raw < 1 else None
        rows.append({
            "have": r.get("have", ""),
            "icon": r.get("haveIconPath"),   # X-side icon = payment currency
            "n": _fmt_rate(raw),
            "inverse": inverse,
            "stack_value": _trim(r.get("stackValue", 0)),
            "total": r.get("total", 0),
        })
    history = result.get("history") or []
    trend = None
    pts = [v for v in history if isinstance(v, (int, float)) and math.isfinite(v)]
    if len(pts) >= 2 and pts[0] != 0:
        trend = round((pts[-1] - pts[0]) / pts[0] * 100, 1)
    return {
        "name": result.get("name", ""),
        "icon": item_icon,
        "one_icon": item_icon,  # header "1 <item>" side = looked-up item
        "stack": _trim(result.get("stack", 1)),
        "rows": rows,
        "history": history,     # oldest->newest floats; brain may omit
        "trend": trend,         # % change first->last, None when insufficient data
    }


def _norm_mod(text: str) -> str:
    """Normalize a mod/stat line for fuzzy matching: strip whole numeric tokens
    (incl. decimals like 8.41), the `#`/`+`/`%` template glyphs, the `:` of
    property templates ("Evasion Rating: #"), and all whitespace, then casefold.
    "37% to Cold Resistance" and "#% to Cold Resistance" both -> "tocoldresistance".
    """
    s = re.sub(r"[+-]?\d+(?:\.\d+)?", "", text)
    s = s.replace("#", "").replace("%", "").replace(":", "")
    return re.sub(r"\s+", "", s).casefold()


def unsearchable_lines(result: dict) -> list[dict]:
    """Parsed item.mods lines that map to NO searchable stat filter.

    item.mods (brain's kind="price" `item`) carries every parsed line as rendered
    display text; `stats[]` carries the searchable lines as templates. A mod line
    whose normalized form matches no stat's normalized form is non-scalable /
    item-specific (e.g. evasion affixes the pipeline collapses into the base
    Evasion total) — surfaced as dim, display-only rows. Returns [{"text": ...}].
    Absent item/mods -> [].
    """
    item = result.get("item")
    if not item:
        return []
    mods = item.get("mods") or {}
    searchable = {_norm_mod(s.get("text", "")) for s in result.get("stats") or []}
    out: list[dict] = []
    for lines in mods.values():
        for line in lines or []:
            text = line.get("text", "")
            if _norm_mod(text) not in searchable:
                out.append({"text": text})
    return out


def prop_rows(result: dict) -> list[dict]:
    """Merged props view: result["props"] entries (kind="prop") followed by
    stats with tag=="property" (kind="stat").  Props get id="p:"+key; property
    stats keep their integer id.  Values/mins pass through _trim when numeric.
    """
    rows: list[dict] = []
    for p in result.get("props") or []:
        text = p.get("text", "")
        val = _num(p.get("value"))
        rows.append({
            "id": "p:" + p.get("key", ""),
            "text": text,
            "label": _prop_label(text, val),
            "value": val,
            "min": _num(p.get("min")),
            "enabled": bool(p.get("enabled")),
            "kind": "prop",
        })
    for s in result.get("stats") or []:
        if s.get("tag") != "property":
            continue
        text = s.get("text", "")
        val = _num(s.get("value"))
        # Property-tagged stats use the "#"-template form (e.g. "Evasion Rating: #").
        rows.append({
            "id": s.get("id", ""),
            "text": text,
            "label": _stat_label(text, val),
            "value": val,
            "min": _num(s.get("min")),
            "enabled": bool(s.get("enabled")),
            "kind": "stat",
        })
    # Corrupted is a base-stat sentinel; always render it last.
    corrupted = [r for r in rows if r["id"] == "p:corrupted"]
    if corrupted:
        rows = [r for r in rows if r["id"] != "p:corrupted"] + corrupted
    return rows
