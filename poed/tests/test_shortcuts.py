from poed.shortcuts import hypr_bind, kwin_trigger, shortcut_modifiers


def test_kwin_trigger_normalizes_case_and_mod_names():
    assert kwin_trigger("ALT+z") == "Alt+Z"
    assert kwin_trigger("control+d") == "Ctrl+D"
    assert kwin_trigger("ESC") == "Esc"


def test_hypr_bind_normalizes_case_and_mod_names():
    assert hypr_bind("ALT+z") == ("ALT", "Z")
    assert hypr_bind("control+d") == ("CTRL", "D")
    assert hypr_bind("ESC") == ("", "ESCAPE")


def test_shortcut_modifiers_returns_xdotool_names():
    assert shortcut_modifiers("ALT+z") == ("alt",)
    assert shortcut_modifiers("control+shift+d") == ("ctrl", "shift")
    assert shortcut_modifiers("ESC") == ()
