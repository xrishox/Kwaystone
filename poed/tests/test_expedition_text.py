"""Catalog name matching edge cases (poed.expedition_text)."""

from __future__ import annotations

from poed.expedition_text import HAVE_MATCH_FLOOR, match_name, parse_compact_count


def test_match_name_strips_gem_level_suffix():
    rows = {
        "Uncut Skill Gem": {"price": 5.0},
        "Thaumaturgic Flux (Level 19)": {"price": 2.0},
    }
    hit = match_name("Uncut Skill Gem (Level 19)", rows, floor=HAVE_MATCH_FLOOR)
    assert hit is not None and hit[0] == "Uncut Skill Gem"
    # Rows that carry the level in their catalog name still exact-match.
    hit = match_name("Thaumaturgic Flux (Level 19)", rows, floor=HAVE_MATCH_FLOOR)
    assert hit is not None and hit[0] == "Thaumaturgic Flux (Level 19)"


def test_level_stripping_does_not_eat_names_ending_in_numbers():
    rows = {"Charm of Level 3": {"price": 1.0}}
    hit = match_name("Charm of Level 3", rows, floor=HAVE_MATCH_FLOOR)
    assert hit is not None and hit[0] == "Charm of Level 3"


def test_parse_compact_count_shared_grammar():
    assert parse_compact_count("1,450").groups == ("1", "450")
    assert parse_compact_count("32.2K").suffix == "K"
    assert parse_compact_count("nonsense") is None


def test_dangling_level_fragment_never_resolves_to_a_specific_level():
    """OCR-truncated '... (Level' must match the base row, not fuzzy-pick
    Level 1's price (the shortest-suffix catalog row)."""
    rows = {
        "Uncut Skill Gem": {"price": 0, "priceAvailable": False},
        "Uncut Skill Gem (Level 1)": {"price": 108.5},
        "Uncut Skill Gem (Level 16)": {"price": 4.6},
    }
    hit = match_name("Uncut Skill Gem (Level", rows, floor=HAVE_MATCH_FLOOR)
    assert hit is not None and hit[0] == "Uncut Skill Gem"
    hit = match_name("Uncut Spirit Gem (Leve", {"Uncut Spirit Gem": {"price": 0}}, floor=HAVE_MATCH_FLOOR)
    assert hit is not None and hit[0] == "Uncut Spirit Gem"
