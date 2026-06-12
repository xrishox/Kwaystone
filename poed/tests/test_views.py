from poed.views import (
    _dominant_currency,
    unique_rows,
    currency_view,
    display_item_card,
    item_card,
    price_rows,
    price_summary,
    prop_rows,
    stat_groups,
    stat_rows,
    unsearchable_lines,
)


def _listing(**kw):
    base = {
        "id": "x",
        "relativeDate": "3 min. ago",
        "priceAmount": 12.0,
        "priceCurrency": "exalted",
        "currencyIconPath": "/tmp/ex.png",
        "isMine": False,
        "accountName": "Foo#123",
        "accountStatus": "online",
        "ign": "CharName",
    }
    base.update(kw)
    return base



def test_dominant_currency_empty_listings():
    assert _dominant_currency([]) == ""


def test_dominant_currency_picks_most_common():
    listings = [
        {"priceCurrency": "exalted"},
        {"priceCurrency": "divine"},
        {"priceCurrency": "exalted"},
    ]
    assert _dominant_currency(listings) == "exalted"


def test_price_summary_empty_listings():
    result = {"total": 53, "id": "abc", "listings": []}
    assert price_summary(result) == {
        "count": "53 listings",
        "min": None,
        "currency": "",
        "icon": None,
    }


def test_price_summary_min_only_no_median():
    result = {
        "total": 4,
        "id": "abc",
        "listings": [
            _listing(priceAmount=8.0, priceCurrency="exalted"),
            _listing(priceAmount=12.0, priceCurrency="exalted"),
            _listing(priceAmount=30.0, priceCurrency="exalted"),
            _listing(priceAmount=2.0, priceCurrency="divine"),
        ],
    }
    s = price_summary(result)
    assert s["count"] == "4 listings"
    assert s["min"] == "8"           # min over dominant currency only
    assert s["currency"] == "exalted"
    assert s["icon"] == "/tmp/ex.png"  # icon of first dominant-currency listing
    assert "med" not in s


def test_price_rows_carry_currency_icon():
    rows = price_rows({"total": 1, "id": "a", "listings": [_listing()]})
    assert rows[0]["icon"] == "/tmp/ex.png"
    assert rows[0]["amount"] == "12"


def test_price_rows_null_icon_falls_back():
    rows = price_rows(
        {"total": 1, "id": "a", "listings": [_listing(currencyIconPath=None)]}
    )
    assert rows[0]["icon"] is None
    assert rows[0]["price"] == "12 exalted"  # text fallback string kept


def test_item_card_groups_ordered_and_empty_hidden():
    item = {
        "name": "Storm Caress",
        "baseType": "Runeforged Stalking Bracers",
        "rarity": "Rare",
        "iconPath": "/tmp/gloves.png",
        "props": [{"text": "Quality", "value": "+20%"}],
        "mods": {
            "rune": [{"text": "+11% to Chaos Resistance"}],
            "implicit": [],
            "prefix": [{"text": "86% increased Evasion Rating", "tier": 2}],
            "suffix": [{"text": "+30 to Dexterity", "tier": 3}],
            "explicit": [],
        },
    }
    card = item_card(item)
    assert card["name"] == "Storm Caress"
    assert card["base"] == "Runeforged Stalking Bracers"
    assert card["icon"] == "/tmp/gloves.png"
    assert [g[0] for g in card["groups"]] == ["Runes", "Prefixes", "Suffixes"]
    prefixes = dict(card["groups"])["Prefixes"]
    assert prefixes[0] == {"text": "86% increased Evasion Rating", "tier": 2}


def test_item_card_missing_item_returns_none():
    assert item_card(None) is None



def test_price_rows_maps_fields_and_ignores_extra():
    result = {
        "total": 1,
        "id": "abc",
        "listings": [_listing(isMine=True, displayItem={"junk": 1})],
    }
    rows = price_rows(result)
    assert rows == [
        {
            "amount": "12",
            "icon": "/tmp/ex.png",
            "price": "12 exalted",
            "seller": "Foo#123",
            "ign": "CharName",
            "age": "3 min. ago",
            "status": "online",
            "mine": True,
            "display_item": {"junk": 1},
        }
    ]



def test_price_rows_tolerates_missing_seller_fields():
    listing = {"priceAmount": 12.0, "priceCurrency": "exalted"}
    rows = price_rows({"total": 1, "id": "abc", "listings": [listing]})
    assert rows == [
        {
            "amount": "12",
            "icon": None,
            "price": "12 exalted",
            "seller": "",
            "ign": "",
            "age": "",
            "status": "offline",
            "mine": False,
            "display_item": None,
        }
    ]



def test_price_rows_tolerate_missing_price_fields():
    rows = price_rows({"total": 1, "id": "a", "listings": [{}]})
    assert rows[0]["amount"] == "0"
    assert rows[0]["price"] == "0 "


def test_price_summary_tolerates_missing_price_amount():
    s = price_summary({"total": 1, "id": "a", "listings": [{"priceCurrency": "exalted"}]})
    assert s["min"] == "0"


def test_currency_view_ge_one_rate():
    # Ninja-style fixed direction: every row reads "1 <item> = X <have>".
    # rawUnit >= 1 -> X = round(rawUnit, 1dp), no inverse hint.
    result = {
        "kind": "currency",
        "name": "Divine Orb",
        "iconPath": "/tmp/d.png",
        "stack": 7,
        "rates": [
            {
                "have": "exalted",
                "haveIconPath": "/tmp/e.png",
                "rawUnit": 85.0,
                "stackValue": 595.0,
                "total": 240,
            },
        ],
    }
    v = currency_view(result)
    assert v["name"] == "Divine Orb"
    assert v["icon"] == "/tmp/d.png"
    assert v["one_icon"] == "/tmp/d.png"   # header: the looked-up item
    assert v["stack"] == "7"
    row = v["rows"][0]
    assert row["n"] == "85"                # X = looked-up-per-have rate
    assert row["icon"] == "/tmp/e.png"     # X-side icon = haveIconPath
    assert row["have"] == "exalted"
    assert row["inverse"] is None          # X >= 1 -> no hint
    assert row["stack_value"] == "595"
    assert row["total"] == 240


def test_currency_view_sub_one_rate_with_inverse():
    # rawUnit < 1 -> X uses 2 significant digits, plus a grey inverse hint
    # "1 <have> = Y <item>" with Y = round(1/raw, 1dp).
    result = {
        "kind": "currency",
        "name": "Chaos Orb",
        "iconPath": "/tmp/c.png",
        "stack": 1,
        "rates": [
            {
                "have": "divine",
                "haveIconPath": "/tmp/dv.png",
                "rawUnit": 0.41,
                "stackValue": 0.41,
                "total": 15,
            },
        ],
    }
    v = currency_view(result)
    assert v["one_icon"] == "/tmp/c.png"   # header still the looked-up item
    row = v["rows"][0]
    assert row["n"] == "0.41"              # 2 significant digits
    assert row["icon"] == "/tmp/dv.png"    # X-side icon = haveIconPath
    assert row["have"] == "divine"
    assert row["inverse"] == "2.4"         # round(1/0.41, 1dp) = 2.4
    assert row["stack_value"] == "0.41"
    assert row["total"] == 15


def test_currency_view_tiny_rate_no_sci_notation():
    # rawUnit very small must not render as scientific notation.
    result = {
        "kind": "currency",
        "name": "Wisdom Scroll",
        "iconPath": "/tmp/w.png",
        "stack": 1,
        "rates": [
            {
                "have": "divine",
                "haveIconPath": "/tmp/dv.png",
                "rawUnit": 0.0055,
                "stackValue": 0.0055,
                "total": 3,
            },
        ],
    }
    row = currency_view(result)["rows"][0]
    assert row["n"] == "0.0055"            # 2 sig digits, decimal form
    assert "e" not in row["n"].lower()     # never scientific notation
    # inverse = round(1/0.0055, 1dp) = round(181.8.., 1) = 181.8
    assert row["inverse"] == "181.8"


def test_currency_view_tolerates_missing_fields():
    v = currency_view({"kind": "currency", "rates": [{}]})
    assert v["name"] == ""
    assert v["stack"] == "1"
    row = v["rows"][0]
    assert row["n"] == "0"                  # absent/zero rawUnit -> "0"
    assert row["inverse"] is None
    assert row["stack_value"] == "0"
    assert row["total"] == 0


def test_currency_view_carries_history():
    result = {
        "kind": "currency",
        "name": "Divine Orb",
        "iconPath": "/tmp/d.png",
        "stack": 1,
        "rates": [],
        "history": [80.0, 82.5, 85.0],
    }
    v = currency_view(result)
    assert v["history"] == [80.0, 82.5, 85.0]


def test_currency_view_history_defaults_empty():
    # Missing key and explicit None both default to [].
    assert currency_view({"kind": "currency", "rates": []})["history"] == []
    assert currency_view({"kind": "currency", "rates": [], "history": None})["history"] == []


def test_currency_view_trend_rising():
    v = currency_view({"kind": "currency", "rates": [], "history": [80.0, 82.5, 88.0]})
    assert v["trend"] == round((88.0 - 80.0) / 80.0 * 100, 1)
    assert v["trend"] > 0


def test_currency_view_trend_falling():
    v = currency_view({"kind": "currency", "rates": [], "history": [100.0, 90.0, 85.0]})
    assert v["trend"] == round((85.0 - 100.0) / 100.0 * 100, 1)
    assert v["trend"] < 0


def test_currency_view_trend_two_points():
    # Exactly 2 points is enough.
    v = currency_view({"kind": "currency", "rates": [], "history": [50.0, 75.0]})
    assert v["trend"] == 50.0


def test_currency_view_trend_none_single_point():
    v = currency_view({"kind": "currency", "rates": [], "history": [80.0]})
    assert v["trend"] is None


def test_currency_view_trend_none_empty():
    v = currency_view({"kind": "currency", "rates": [], "history": []})
    assert v["trend"] is None


def test_currency_view_trend_none_first_zero():
    # Division by zero: first point is 0 -> trend must be None.
    v = currency_view({"kind": "currency", "rates": [], "history": [0.0, 10.0]})
    assert v["trend"] is None


def test_currency_view_trend_none_missing_history():
    # No history key -> trend is None.
    v = currency_view({"kind": "currency", "rates": []})
    assert v["trend"] is None


def test_display_item_card_groups_api_categories():
    di = {
        "title": ["Storm Caress", "Runeforged Stalking Bracers"],
        "enchantMods": [{"text": "enchant line", "color": 0}],
        "runeMods": [{"text": "+11% to Chaos Resistance", "color": 0}],
        "implicitMods": [{"text": "implicit line", "color": 0}],
        "explicitMods": [{"text": "86% increased Evasion Rating", "color": 0}],
        "fracturedMods": [],
        "desecratedMods": None,
    }
    card = display_item_card(di)
    assert card["name"] == "Storm Caress"
    assert [g[0] for g in card["groups"]] == ["Enchants", "Runes", "Implicits", "Mods"]
    assert dict(card["groups"])["Runes"][0]["text"] == "+11% to Chaos Resistance"


def test_display_item_card_full_shape():
    # title -> name/base, iconPath -> icon, itemProps -> props, groups from mods.
    di = {
        "title": ["Storm Caress", "Runeforged Stalking Bracers"],
        "iconPath": "/tmp/gloves.png",
        "itemProps": [
            {"text": "Evasion Rating", "value": "420", "color": 0},
            {"text": "Item Level", "value": "80", "color": 0},
        ],
        "runeMods": [{"text": "+11% to Chaos Resistance", "color": 0}],
    }
    card = display_item_card(di)
    assert card["name"] == "Storm Caress"
    assert card["base"] == "Runeforged Stalking Bracers"
    assert card["icon"] == "/tmp/gloves.png"
    assert card["props"] == [
        {"text": "Evasion Rating", "value": "420"},
        {"text": "Item Level", "value": "80"},
    ]
    assert dict(card["groups"])["Runes"][0]["text"] == "+11% to Chaos Resistance"


def test_display_item_card_props_value_defaults_empty_and_skips_textless():
    # Missing value -> "", entries with no text dropped.
    di = {
        "title": ["Foo", "Bar"],
        "itemProps": [
            {"text": "Quality"},          # no value -> ""
            {"value": "lost"},            # no text -> skipped
            {"text": "Armour", "value": "300"},
        ],
    }
    card = display_item_card(di)
    assert card["props"] == [
        {"text": "Quality", "value": ""},
        {"text": "Armour", "value": "300"},
    ]


def test_display_item_card_degrades_cleanly():
    # No title -> name "item", base ""; no iconPath -> None; no itemProps -> [].
    card = display_item_card({})
    assert card["name"] == "item"
    assert card["base"] == ""
    assert card["icon"] is None
    assert card["props"] == []
    assert card["groups"] == []
    # title with a single element -> base ""
    single = display_item_card({"title": ["OnlyName"]})
    assert single["name"] == "OnlyName"
    assert single["base"] == ""


def test_display_item_card_text_value_concat_and_empty():
    di = {"title": None, "explicitMods": [{"text": "+#% to X ", "value": 37}]}
    card = display_item_card(di)
    assert dict(card["groups"])["Mods"][0]["text"] == "+#% to X 37"
    assert display_item_card({})["groups"] == []


def test_stat_rows_built_from_response():
    result = {"kind": "price", "total": 1, "id": "a", "listings": [],
              "stats": [
                  {"id": 0, "text": "+# to Dexterity", "value": 30,
                   "min": 27, "max": None, "enabled": True, "tag": "explicit"},
                  {"id": 1, "text": "+#% to Chaos Resistance", "value": 11,
                   "min": None, "max": None, "enabled": False, "tag": "rune"},
              ]}
    rows = stat_rows(result)
    assert rows[0] == {"id": 0, "text": "+# to Dexterity", "label": "+30 to Dexterity",
                       "value": "30", "min": "27", "enabled": True, "tag": "explicit"}
    assert rows[1]["enabled"] is False
    assert rows[1]["min"] is None
    assert rows[1]["tag"] == "rune"
    # value present -> "#" substituted with the rolled value
    assert rows[1]["label"] == "+11% to Chaos Resistance"


def test_stat_rows_label_substitutes_value():
    result = {"kind": "price", "total": 0, "id": "a", "listings": [], "stats": [
        {"id": 0, "text": "# to Dexterity", "value": 30, "min": None,
         "max": None, "enabled": True, "tag": "explicit"},
    ]}
    assert stat_rows(result)[0]["label"] == "30 to Dexterity"


def test_stat_rows_label_valueless_strips_placeholder():
    # No value -> drop the "#" placeholder and collapse leftover whitespace.
    result = {"kind": "price", "total": 0, "id": "a", "listings": [], "stats": [
        {"id": 0, "text": "# to maximum Life", "value": None, "min": None,
         "max": None, "enabled": False, "tag": "explicit"},
    ]}
    assert stat_rows(result)[0]["label"] == "to maximum Life"


def test_stat_rows_absent_key():
    assert stat_rows({"kind": "price", "total": 0, "id": "a", "listings": []}) == []


def test_stat_groups_by_tag():
    result = {"kind": "price", "total": 0, "id": "a", "listings": [], "stats": [
        {"id": 0, "text": "+# total to Cold Resistance", "value": 37,
         "min": 33, "max": None, "enabled": True, "tag": "pseudo"},
        {"id": 1, "text": "# to Dexterity", "value": 30, "min": 27,
         "max": None, "enabled": True, "tag": "explicit"},
        {"id": 2, "text": "+#% to Chaos Resistance", "value": 11, "min": None,
         "max": None, "enabled": False, "tag": "rune"},
    ]}
    groups = stat_groups(result)
    assert [g[0] for g in groups] == ["Pseudo", "Mods", "Runes"]
    assert groups[1][1][0]["id"] == 1


def test_stat_groups_excludes_property_tag():
    # "property"-tagged stats go to prop_rows, not stat_groups.
    result = {"kind": "price", "total": 0, "id": "a", "listings": [], "stats": [
        {"id": 0, "text": "Evasion Rating: #", "value": 420, "min": 400,
         "max": None, "enabled": True, "tag": "property"},
        {"id": 1, "text": "+# to Dexterity", "value": 30, "min": 27,
         "max": None, "enabled": True, "tag": "explicit"},
    ]}
    groups = stat_groups(result)
    labels = [g[0] for g in groups]
    assert "Mods" in labels
    # "Mods" group must NOT contain the property row
    mods = dict(groups)["Mods"]
    assert all(r["tag"] != "property" for r in mods)


def test_prop_rows_merges_props_and_property_stats():
    # result["props"] entries get id="p:"+key; "property"-tagged stats get id=int index.
    # Props come first, then stats.
    result = {
        "kind": "price",
        "props": [
            {"key": "quality", "text": "Quality", "value": 20, "min": None, "enabled": True},
            {"key": "itemLevel", "text": "Item Level", "value": 80, "min": 75, "enabled": True},
        ],
        "stats": [
            {"id": 0, "text": "Evasion Rating: #", "value": 420, "min": 400,
             "max": None, "enabled": True, "tag": "property"},
            {"id": 1, "text": "+# to Dexterity", "value": 30, "min": 27,
             "max": None, "enabled": True, "tag": "explicit"},
        ],
    }
    rows = prop_rows(result)
    # Should have 3 entries: 2 from props + 1 from property-tagged stat
    assert len(rows) == 3

    # First two: from result["props"], kind="prop"
    assert rows[0] == {
        "id": "p:quality",
        "text": "Quality",
        "label": "Quality 20",  # props have no "#": label = "text value"
        "value": "20",
        "min": None,
        "enabled": True,
        "kind": "prop",
    }
    assert rows[1] == {
        "id": "p:itemLevel",
        "text": "Item Level",
        "label": "Item Level 80",
        "value": "80",
        "min": "75",
        "enabled": True,
        "kind": "prop",
    }

    # Third: from property-tagged stat, kind="stat"
    assert rows[2] == {
        "id": 0,
        "text": "Evasion Rating: #",
        "label": "Evasion Rating: 420",  # "#" substituted with the value
        "value": "420",
        "min": "400",
        "enabled": True,
        "kind": "stat",
    }


def test_prop_rows_empty_when_no_props():
    result = {"kind": "price", "stats": [
        {"id": 0, "text": "+# to Dexterity", "value": 30, "min": 27,
         "max": None, "enabled": True, "tag": "explicit"},
    ]}
    assert prop_rows(result) == []


def test_prop_rows_tolerates_missing_result_props():
    # result has no "props" key and no property-tagged stats
    assert prop_rows({}) == []


def test_prop_rows_numeric_trim():
    result = {
        "props": [{"key": "armour", "text": "Armour", "value": 500.0, "min": None, "enabled": False}],
        "stats": [],
    }
    rows = prop_rows(result)
    assert rows[0]["value"] == "500"
    assert rows[0]["enabled"] is False
    assert rows[0]["label"] == "Armour 500"  # text + value


def test_prop_rows_valueless_label_is_text_only():
    result = {
        "props": [{"key": "foo", "text": "Some Flag", "value": None, "min": None, "enabled": True}],
        "stats": [],
    }
    assert prop_rows(result)[0]["label"] == "Some Flag"


def test_prop_rows_corrupted_last():
    # Corrupted is a sentinel base stat that must always render at the end,
    # regardless of where it appears in result["props"].
    # Order: props=[corrupted, itemLevel], stats=[property-tagged stat]
    # Expected render order: [itemLevel, property-stat, corrupted]
    result = {
        "props": [
            {"key": "corrupted", "text": "Corrupted", "value": None, "min": None, "enabled": True},
            {"key": "itemLevel", "text": "Item Level", "value": 80, "min": None, "enabled": True},
        ],
        "stats": [
            {"id": 0, "text": "Evasion Rating: #", "value": 420, "min": None,
             "max": None, "enabled": True, "tag": "property"},
        ],
    }
    rows = prop_rows(result)
    assert len(rows) == 3
    assert [r["id"] for r in rows] == ["p:itemLevel", 0, "p:corrupted"]


def test_unsearchable_lines_excludes_matched_includes_nonscalable():
    # item.mods carries rendered text (numbers filled in); stats[] carries the
    # template form. Normalization (strip numbers/#/+/%/punct/whitespace,
    # casefold) makes a matched line collapse to the same key as its stat.
    result = {
        "item": {
            "mods": {
                "rune": [],
                "implicit": [],
                "prefix": [{"text": "37 to maximum Life"}],  # matches a stat
                "suffix": [
                    {"text": "30 to Dexterity"},  # matches a stat
                    {"text": "157 to Evasion Rating"},  # no matching stat -> shown
                ],
                "explicit": [],
            }
        },
        "stats": [
            {"text": "# to maximum Life"},
            {"text": "# to Dexterity"},
        ],
    }
    lines = unsearchable_lines(result)
    assert lines == [{"text": "157 to Evasion Rating"}]


def test_unsearchable_lines_decimal_tokens_match():
    # Decimal rolls (8.41) must be stripped whole so the line still matches the
    # template stat — a naive digit-only strip leaves a stray "." and misses.
    result = {
        "item": {
            "mods": {
                "suffix": [
                    {"text": "Leech 8.41% of Physical Attack Damage as Life"},
                ],
            }
        },
        "stats": [{"text": "Leech #% of Physical Attack Damage as Life"}],
    }
    assert unsearchable_lines(result) == []


def test_unsearchable_lines_absent_item_or_mods_is_empty():
    assert unsearchable_lines({}) == []
    assert unsearchable_lines({"item": None}) == []
    assert unsearchable_lines({"item": {}}) == []
    assert unsearchable_lines({"item": {"mods": {}}, "stats": []}) == []


def test_unique_rows_sorted_and_flagged():
    matches = [
        {"name": "Cheap Ring", "price": 0.4, "quantity": 900, "kind": "unique",
         "ambiguous": False, "score": 0.93, "x": 1, "y": 1, "w": 8, "h": 8},
        {"name": "Mageblood", "price": 67305.6, "quantity": 1386, "kind": "unique",
         "ambiguous": False, "score": 0.91, "x": 9, "y": 9, "w": 8, "h": 8},
        {"name": "Gem (Level 1) +19", "price": 90.0, "quantity": 5, "kind": "tagged",
         "ambiguous": True, "score": 0.88, "x": 20, "y": 20, "w": 8, "h": 8},
    ]
    rows = unique_rows(matches, min_exalted=1.0)
    assert [r["name"] for r in rows] == ["Mageblood", "Gem (Level 1) +19", "Cheap Ring"]
    assert [r["good"] for r in rows] == [True, True, False]
    assert rows[0]["price"] == "67306"
    # Ambiguous (shared-art group) prices carry a "?" suffix.
    assert rows[1]["price"] == "90?"
    assert rows[2]["price"] == "0.4"


def test_unique_rows_trend_arrows():
    def m(trend):
        return {"name": "X", "price": 5.0, "quantity": 1, "kind": "unique",
                "ambiguous": False, "trend": trend}
    assert unique_rows([m(0.25)], 1.0)[0]["trend"] == "↗"
    assert unique_rows([m(-0.3)], 1.0)[0]["trend"] == "↘"
    assert unique_rows([m(0.05)], 1.0)[0]["trend"] == ""
    assert unique_rows([m(None)], 1.0)[0]["trend"] == ""


def test_skill_group_renders_above_implicits():
    """Granted skills feel odd buried in Mods; they render right after Runes."""
    from poed.views import item_card

    card = item_card({
        "name": "X", "mods": {
            "skill": [{"text": "Grants Skill: Level 18 Spirit Vessel"}],
            "implicit": [{"text": "imp"}],
            "explicit": [{"text": "mod"}],
        },
    })
    labels = [label for label, _ in card["groups"]]
    assert "Skill" in labels
    assert labels.index("Skill") < labels.index("Implicits")


def test_skill_group_renders_first():
    from poed.views import item_card

    card = item_card({
        "name": "X", "mods": {
            "skill": [{"text": "s"}], "rune": [{"text": "r"}],
            "implicit": [{"text": "i"}], "explicit": [{"text": "m"}],
        },
    })
    assert [label for label, _ in card["groups"]][0] == "Skill"
