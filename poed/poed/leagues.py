"""League-list hygiene for the control-window selector.

Pure helpers (no GTK) so the auto-follow decision is unit-testable: when the
tracked league ends, the selector follows the newest current league in the
same family (softcore -> softcore, Hardcore -> Hardcore), else the family's
permanent league.
"""

# Permanent leagues are always trackable even though the API flags them
# IsCurrent=false (that flag means "current challenge league").
PERMANENT = ("Standard", "Hardcore")


def family(name: str) -> str:
    """'hc' for Hardcore-flavoured leagues, 'sc' otherwise."""
    return "hc" if name == "Hardcore" or name.startswith("HC ") else "sc"


def follow_target(
    available: list[str], current: dict[str, bool], previous: str
) -> str:
    """Where to move when `previous` is no longer in the active list.

    The API orders leagues oldest-first, so the newest current league in the
    family is the LAST current family match; the family's permanent league is
    the fallback when nothing current exists in it.
    """
    fam = family(previous)
    for name in reversed(available):
        if current.get(name) and family(name) == fam:
            return name
    return "Hardcore" if fam == "hc" else "Standard"
