from poed.views import (
    match_badge_label,
    match_badge_name_label,
    match_badge_price_label,
    match_name_abbreviation,
    match_total_value,
    match_value_label,
    screen_scan_route_label,
    value_tier_css,
)


def test_screen_scan_route_label_uses_scanner_id():
    assert screen_scan_route_label("have", []) == "have"
    assert screen_scan_route_label("combination", [
        {"scanKind": "expedition"},
        {"scanKind": "runeshape"},
    ]) == "combination"
    assert screen_scan_route_label(None, []) == "none"


def test_screen_scan_route_label_identifies_multi_rune():
    assert screen_scan_route_label("runeshape", [
        {"scanKind": "runeshape", "name": "A"},
        {"scanKind": "runeshape", "name": "B"},
        {"scanKind": "runeshape", "markerOnly": True},
    ]) == "multi-rune"


def test_screen_scan_route_label_keeps_single_runeshape():
    assert screen_scan_route_label("runeshape", [
        {"scanKind": "runeshape", "name": "A"},
    ]) == "runeshape"


def test_match_value_label_shows_stack_total():
    match = {
        "price": 1.0,
        "unitPrice": 1.0,
        "totalPrice": 2.0,
        "stackSize": 2,
        "priceCurrency": "exalted",
    }

    assert match_value_label(match, compact=True) == "1ex (2x = 2ex)"
    assert match_value_label(match) == "1 ex (2x = 2 ex)"
    assert match_badge_price_label(match) == "2 ex"


def test_match_value_label_shows_native_market_quote_with_ex_conversion():
    match = {
        "price": 7.1,
        "unitPrice": 7.1,
        "totalPrice": 7.1,
        "priceCurrency": "exalted",
        "quoteAmount": 1 / 3,
        "quoteCurrency": "chaos",
        "quoteCurrencyText": "Chaos Orb",
        "quoteAvailable": True,
    }

    assert match_value_label(match) == "0.33 c (~7.1 ex)"
    assert match_badge_price_label(match) == "7.1 ex"


def test_match_value_label_preserves_small_native_quote_precision():
    match = {
        "price": 1,
        "unitPrice": 1,
        "totalPrice": 1,
        "priceCurrency": "exalted",
        "quoteAmount": 0.0050466,
        "quoteCurrency": "divine",
        "quoteCurrencyText": "Divine Orb",
        "quoteAvailable": True,
    }

    assert match_value_label(match) == "0.005 div (~1 ex)"
    assert match_badge_price_label(match) == "1 ex"


def test_match_badge_price_label_uses_exalted_for_grey_tier_even_with_tiny_div_quote():
    match = {
        "price": 0.25,
        "unitPrice": 0.25,
        "totalPrice": 0.25,
        "priceCurrency": "exalted",
        "quoteAmount": 0.0008,
        "quoteCurrency": "divine",
        "quoteCurrencyText": "Divine Orb",
        "quoteAvailable": True,
        "exaltedPerDivine": 300,
    }

    assert value_tier_css(match_total_value(match)) == "poe-value-grey"
    assert match_badge_price_label(match) == "0.25 ex"


def test_match_value_label_marks_unavailable_market_and_converts_stack():
    match = {
        "price": 2.5,
        "unitPrice": 2.5,
        "totalPrice": 10,
        "stackSize": 4,
        "priceCurrency": "exalted",
        "quoteAmount": 2.5,
        "quoteCurrency": "exalted",
        "quoteCurrencyText": "Exalted Orb",
        "quoteAvailable": False,
    }

    assert match_value_label(match) == "2.5 ex (4x = 10 ex; no buyers)"
    assert match_badge_price_label(match) == "10 ex"


def test_match_badge_price_label_uses_chaos_for_orange_tier_from_matching_quote():
    match = {
        "unitPrice": 50,
        "totalPrice": 150,
        "stackSize": 3,
        "priceCurrency": "exalted",
        "quoteAmount": 200,
        "quoteCurrency": "chaos",
        "quoteCurrencyText": "Chaos Orb",
        "exaltedPerChaos": 0.25,
    }

    assert value_tier_css(match_total_value(match)) == "poe-value-orange"
    assert match_badge_price_label(match) == "600 c"


def test_match_badge_price_label_converts_orange_tier_to_chaos_when_quote_differs():
    match = {
        "unitPrice": 150,
        "totalPrice": 150,
        "priceCurrency": "exalted",
        "quoteAmount": 0.5,
        "quoteCurrency": "divine",
        "quoteCurrencyText": "Divine Orb",
        "exaltedPerChaos": 0.25,
        "exaltedPerDivine": 300,
    }

    assert value_tier_css(match_total_value(match)) == "poe-value-orange"
    assert match_badge_price_label(match) == "600 c"


def test_match_badge_price_label_uses_divine_for_white_tier():
    match = {
        "unitPrice": 333,
        "totalPrice": 333,
        "priceCurrency": "exalted",
        "quoteAmount": 1,
        "quoteCurrency": "divine",
        "quoteCurrencyText": "Divine Orb",
        "exaltedPerDivine": 333,
    }

    assert value_tier_css(match_total_value(match)) == "poe-value-white"
    assert match_badge_price_label(match) == "1 div"


def test_match_badge_price_label_converts_white_tier_to_divine_when_quote_differs():
    match = {
        "unitPrice": 666,
        "totalPrice": 666,
        "priceCurrency": "exalted",
        "quoteAmount": 2664,
        "quoteCurrency": "chaos",
        "quoteCurrencyText": "Chaos Orb",
        "exaltedPerChaos": 0.25,
        "exaltedPerDivine": 333,
    }

    assert value_tier_css(match_total_value(match)) == "poe-value-white"
    assert match_badge_price_label(match) == "2 div"


def test_match_name_abbreviation_uses_first_two_letters_of_each_word():
    assert match_name_abbreviation("Greater Iron Rune") == "Gr Ir Ru"
    assert match_name_abbreviation("Atziri's Temple") == "At Te"


def test_match_badge_label_prefixes_recognized_name_abbreviation():
    match = {
        "name": "Greater Iron Rune",
        "price": 5,
        "priceCurrency": "exalted",
    }

    assert match_badge_label(match) == "Gr Ir Ru · 5 ex"


def test_match_badge_label_uses_stack_and_full_name_for_runeshape():
    match = {
        "scanKind": "runeshape",
        "name": "Greater Orb of Augmentation",
        "runeshapeLevel": "Lv70+",
        "price": 2,
        "unitPrice": 2,
        "stackSize": 3,
        "totalPrice": 6,
    }

    assert match_badge_name_label(match) == "3x Greater Orb of Augmentation Lv70+"
    assert match_badge_label(match) == "3x Greater Orb of Augmentation Lv70+ · 6 ex"


def test_match_badge_label_disambiguates_generic_runeshape_unique_with_level():
    match = {
        "scanKind": "runeshape",
        "name": "Unique",
        "runeshapeLevel": "Lv65-74",
    }

    assert match_badge_name_label(match) == "Unique Lv65-74"


def test_match_badge_label_uses_full_name_for_expedition():
    match = {
        "scanKind": "expedition",
        "name": "Greater Orb of Augmentation",
        "price": 2,
        "unitPrice": 2,
        "stackSize": 3,
        "totalPrice": 6,
    }

    assert match_badge_name_label(match) == "3x Greater Orb of Augmentation"
    assert match_badge_label(match) == "3x Greater Orb of Augmentation · 6 ex"


def test_match_badge_label_uses_full_name_for_have():
    match = {
        "scanKind": "have",
        "name": "Greater Iron Rune",
        "price": 2,
        "unitPrice": 2,
        "stackSize": 12,
        "totalPrice": 24,
    }

    assert match_badge_name_label(match) == "12x Greater Iron Rune"
    assert match_badge_label(match) == "12x Greater Iron Rune · 24 ex"


def test_match_badge_label_prefixes_stack_for_abbreviated_items():
    match = {
        "name": "Greater Iron Rune",
        "price": 5,
        "unitPrice": 5,
        "totalPrice": 15,
        "stackSize": 3,
        "priceCurrency": "exalted",
    }

    assert match_badge_name_label(match) == "3x Gr Ir Ru"
    assert match_badge_label(match) == "3x Gr Ir Ru · 15 ex"


def test_match_value_label_marks_missing_market_price():
    assert match_value_label({"price": 0, "priceAvailable": False}) == "no market price"
    assert match_badge_price_label({"price": 0, "priceAvailable": False}) == ""
    assert match_badge_label({
        "name": "Greater Iron Rune",
        "price": 0,
        "priceAvailable": False,
    }) == "Gr Ir Ru"
    assert match_badge_label({"markerOnly": True}) == "unrecognized reward"


def test_value_tier_css_uses_inclusive_thresholds():
    assert value_tier_css(0.9) == "poe-value-grey"
    assert value_tier_css(1) == "poe-value-blue"
    assert value_tier_css(50) == "poe-value-red"
    assert value_tier_css(100) == "poe-value-orange"
    assert value_tier_css(300) == "poe-value-white"


def test_low_confidence_prices_render_as_estimates():
    """Thin-volume exchange prices display as ~estimates, never as
    confident divine-plus values (lineage supports/idols class)."""
    match = {
        "name": "Garukhan's Resolve",
        "price": 396260.0,
        "unitPrice": 396260.0,
        "stackSize": 1,
        "priceAvailable": True,
        "priceConfidence": "low",
        "quoteAmount": 545.74,
        "quoteCurrency": "divine",
        "quoteCurrencyText": "Divine Orb",
        "quoteAvailable": True,
        "exaltedPerDivine": 726.0,
    }
    label = match_value_label(match)
    assert label.startswith("~")
    assert "?" in label
    assert "illiquid" in label

    match["priceConfidence"] = "ok"
    assert "illiquid" not in match_value_label(match)
    assert not match_value_label(match).startswith("~")
