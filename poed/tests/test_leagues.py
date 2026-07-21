from poed import leagues


def test_family_classifies_hardcore_variants():
    assert leagues.family("Standard") == "sc"
    assert leagues.family("Runes of Aldur") == "sc"
    assert leagues.family("Hardcore") == "hc"
    assert leagues.family("HC Runes of Aldur") == "hc"


def test_follow_target_follows_newest_current_in_family():
    available = ["Standard", "Hardcore", "Fate of the Vaal", "HC Fate of the Vaal"]
    current = {name: name != "Standard" and name != "Hardcore" for name in available}

    # A dead softcore challenge league follows the new softcore challenge league.
    assert leagues.follow_target(available, current, "Runes of Aldur") == "Fate of the Vaal"
    # A dead HC challenge league follows the new HC challenge league.
    assert leagues.follow_target(available, current, "HC Runes of Aldur") == "HC Fate of the Vaal"


def test_follow_target_falls_back_to_permanent_family_league():
    available = ["Standard", "Hardcore"]
    current = {"Standard": False, "Hardcore": False}

    assert leagues.follow_target(available, current, "Runes of Aldur") == "Standard"
    assert leagues.follow_target(available, current, "HC Runes of Aldur") == "Hardcore"
