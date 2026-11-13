"""``Setting.accepts``: only an enum says no."""

from __future__ import annotations

import pytest

from igni_core.extensions.manifest import Setting


def _setting(type_: str, **more) -> Setting:
    return Setting.model_validate({"key": "mode", "type": type_, "label": "Mode", **more})


def test_an_enum_accepts_its_options_and_nothing_else():
    setting = _setting("enum", options=["fast", "slow"])
    assert setting.accepts("fast") is True
    assert setting.accepts("slow") is True
    assert setting.accepts("medium") is False
    assert setting.accepts("FAST") is False
    assert setting.accepts(None) is False
    assert setting.accepts(1) is False


@pytest.mark.parametrize("type_", ["string", "number", "boolean"])
@pytest.mark.parametrize("value", ["x", 3, True, None, ["a"]])
def test_the_other_kinds_accept_anything(type_, value):
    assert _setting(type_).accepts(value) is True
