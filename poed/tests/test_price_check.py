import pytest


try:
    from poed import price_check
except (ImportError, ValueError) as exc:  # pragma: no cover - depends on GTK stack
    pytest.skip(f"price-check UI unavailable: {exc}", allow_module_level=True)


class _Brain:
    def request(self, *_args, **_kwargs):
        raise AssertionError("copy failure must not contact the EE2 host")


class _Desktop:
    def is_game_focused(self):
        return True


def test_copy_failure_does_not_show_native_ui(monkeypatch):
    monkeypatch.setattr(price_check.clipboard, "grab_item_text", lambda *_args: None)
    visibility_changes = []
    controller = price_check.PriceCheckController(
        application=object(),
        cfg={
            "hotkey_price": "ALT+z",
            "league": "Runes of Aldur",
            "account_name": "",
            "poesessid": "",
        },
        brain=_Brain(),
        desktop=_Desktop(),
        on_visibility_changed=lambda: visibility_changes.append(True),
    )

    controller.run(1, lambda gen: gen == 1)

    assert visibility_changes == []
    assert not controller.is_visible()
