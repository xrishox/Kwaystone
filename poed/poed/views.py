"""Pure view-model builders: brain JSON results -> panel data. No GTK."""
# All listing/offer reads are defensive: the live trade API omits fields freely (a missing "ign" crashed the panel once).

import math
import re


def _trim(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else str(n)


def _value_amount(n: float) -> str:
    return _trim(round(n)) if n >= 10 else _trim(round(n, 1))


def _quote_amount(n: float) -> str:
    if n >= 10:
        digits = 0
    elif n >= 1:
        digits = 1
    elif n >= 0.1:
        digits = 2
    elif n >= 0.01:
        digits = 3
    else:
        digits = 4
    return _trim(round(n, digits))


def _currency_short(name: str) -> str:
    key = str(name or "exalted").lower()
    return {
        "exalted": "ex",
        "exalt": "ex",
        "ex": "ex",
        "divine": "div",
        "div": "div",
        "chaos": "c",
        "c": "c",
    }.get(key, key)


def _currency_key(name: str | None) -> str:
    key = str(name or "").strip().lower()
    return {
        "exalted": "exalted",
        "exalt": "exalted",
        "ex": "exalted",
        "exalted orb": "exalted",
        "divine": "divine",
        "div": "divine",
        "divine orb": "divine",
        "chaos": "chaos",
        "c": "chaos",
        "chaos orb": "chaos",
    }.get(key, key)


def _market_currency_short(api_id: str, text: str) -> str:
    known = _currency_short(api_id)
    if known != str(api_id or "").lower():
        return known
    words = re.findall(r"[A-Za-z0-9]+", str(text or api_id or ""))
    if words and words[-1].lower() == "orb":
        words = words[:-1]
    return " ".join(word[:3].lower() for word in words) or known


def match_total_value(match: dict) -> float:
    price = match.get("price") or 0
    return match.get("totalPrice") or price


def value_tier_css(value: float) -> str:
    if value >= 300:
        return "poe-value-white"
    if value >= 100:
        return "poe-value-orange"
    if value >= 50:
        return "poe-value-red"
    if value >= 1:
        return "poe-value-blue"
    return "poe-value-grey"


def _badge_target_currency(value: float) -> str:
    if value >= 300:
        return "divine"
    if value >= 100:
        return "chaos"
    return "exalted"


def _numeric(value) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _exalted_per(match: dict, currency: str) -> float | None:
    if currency == "exalted":
        return 1.0
    key = {
        "chaos": "exaltedPerChaos",
        "divine": "exaltedPerDivine",
    }.get(currency)
    if key is None:
        return None
    rate = _numeric(match.get(key))
    return rate if rate is not None and rate > 0 else None


def match_value_label(match: dict, *, compact: bool = False) -> str:
    if match.get("markerOnly"):
        return ""
    if match.get("priceAvailable") is False:
        return "no market price"
    low_confidence = match.get("priceConfidence") == "low"
    sep = "" if compact else " "
    price = match.get("unitPrice", match.get("price") or 0) or 0
    total = match.get("totalPrice")
    try:
        stack = int(match.get("stackSize") or 1)
    except (TypeError, ValueError):
        stack = 1
    if total is None and stack > 1:
        total = price * stack
    cur = _currency_short(match.get("priceCurrency", "exalted"))
    suffix = "?" if match.get("ambiguous") or low_confidence else ""
    prefix = "~" if low_confidence else ""
    quote = match.get("quoteAmount")
    quote_currency = match.get("quoteCurrency")
    if isinstance(quote, (int, float)) and quote > 0 and quote_currency:
        qcur = _market_currency_short(
            str(quote_currency),
            str(match.get("quoteCurrencyText") or quote_currency),
        )
        quote_base = f"{prefix}{_quote_amount(quote)}{suffix}{sep}{qcur}"
        available = match.get("quoteAvailable")
        no_buyers = available is False
        if stack > 1:
            quote_total = quote * stack
            ex_total = total if total is not None else price * stack
            details = (
                f"{stack}x = {_quote_amount(quote_total)}{suffix}{sep}{qcur}"
            )
            if qcur != cur:
                details += f"; ~{_value_amount(ex_total)}{suffix}{sep}{cur}"
            if no_buyers:
                details += "; no buyers"
            elif low_confidence:
                details += "; illiquid"
            return f"{quote_base} ({details})"
        if qcur != cur:
            details = f"~{_value_amount(price)}{suffix}{sep}{cur}"
            if no_buyers:
                details += "; no buyers"
            elif low_confidence:
                details += "; illiquid"
            return f"{quote_base} ({details})"
        if no_buyers:
            return f"{quote_base} (no buyers)"
        if low_confidence:
            return f"{quote_base} (illiquid)"
        return quote_base

    base = f"{prefix}{_value_amount(price)}{suffix}{sep}{cur}"
    if stack > 1 and total is not None and total > price:
        return f"{base} ({stack}x = {_value_amount(total)}{suffix}{sep}{cur})"
    return base


def match_badge_price_label(match: dict) -> str:
    """Compact on-screen badge value.

    The scan panel can show detailed stack/conversion/no-buyer context. The
    badge over the item stays deterministic by value tier: grey/blue/red show
    Exalted, orange shows Chaos, white shows Divine. A native quote in the
    desired currency is used when present; otherwise the exalted-equivalent
    value is converted with league exchange rates carried on the match.
    """

    if match.get("markerOnly") or match.get("priceAvailable") is False:
        return ""
    low_confidence = match.get("priceConfidence") == "low"
    suffix = "?" if match.get("ambiguous") or low_confidence else ""
    prefix = "~" if low_confidence else ""
    try:
        stack = int(match.get("stackSize") or 1)
    except (TypeError, ValueError):
        stack = 1

    value = match_total_value(match)
    target = _badge_target_currency(value)
    quote = _numeric(match.get("quoteAmount"))
    quote_currency = _currency_key(match.get("quoteCurrency"))
    if quote is not None and quote > 0 and quote_currency == target:
        amount = quote * stack if stack > 1 else quote
        return f"{prefix}{_quote_amount(amount)}{suffix} {_currency_short(target)}"

    rate = _exalted_per(match, target)
    if rate is not None:
        return f"{prefix}{_quote_amount(value / rate)}{suffix} {_currency_short(target)}"

    # Missing conversion metadata should not reintroduce random native quote
    # currencies. Fall back to the exalted-equivalent anchor.
    if target == "exalted":
        return f"{prefix}{_quote_amount(value)}{suffix} ex"
    return f"{prefix}{_value_amount(value)}{suffix} ex"


def match_name_abbreviation(name: str) -> str:
    """First two letters of every word in a recognized item name."""
    words = re.findall(r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)*", str(name or ""))
    return " ".join(word[:2] for word in words)


def _match_stack_size(match: dict) -> int:
    try:
        return max(1, int(match.get("stackSize") or 1))
    except (TypeError, ValueError):
        return 1


_FULL_NAME_BADGE_KINDS = frozenset({"expedition", "have", "runeshape"})


def match_display_name(match: dict) -> str:
    name = str(match.get("name") or "")
    if (
        name
        and str(match.get("scanKind") or "") == "runeshape"
        and match.get("runeshapeLevel")
    ):
        name = f"{name} {match['runeshapeLevel']}"
    return name


def match_badge_name_label(match: dict) -> str:
    if match.get("markerOnly"):
        return ""
    scan_kind = str(match.get("scanKind") or "")
    display_name = match_display_name(match)
    name = (
        display_name
        if scan_kind in _FULL_NAME_BADGE_KINDS
        else match_name_abbreviation(display_name)
    )
    if not name:
        return ""
    stack = _match_stack_size(match)
    return f"{stack}x {name}" if stack > 1 else name


def screen_scan_route_label(scanner_id: str | None, matches: list[dict]) -> str:
    """Human-facing route/category label for the transient scan overlay."""
    route = str(scanner_id or "none").strip() or "none"
    if route == "runeshape":
        runeshape_matches = [
            match for match in matches
            if match.get("scanKind") == "runeshape" and not match.get("markerOnly")
        ]
        if len(runeshape_matches) > 1:
            return "multi-rune"
    return route


def match_badge_label(match: dict) -> str:
    if match.get("markerOnly"):
        return "unrecognized reward"
    name = match_badge_name_label(match)
    value = match_badge_price_label(match)
    if name and value:
        return f"{name} · {value}"
    return name or value
