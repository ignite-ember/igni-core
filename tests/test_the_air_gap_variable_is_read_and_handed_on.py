"""``IGNI_AIR_GAPPED`` only ever turns air-gapped mode on, and is handed to children only by a real ``True``.

``core/air_gap.py``.
"""

from __future__ import annotations

import pytest

from igni_core.core.air_gap import AIR_GAPPED_ENV, air_gapped_by_environment, export_to


class TestReadingTheVariable:
    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", " YES ", "True", "\tOn\n"])
    def test_these_spellings_are_on(self, value: str):
        assert air_gapped_by_environment({AIR_GAPPED_ENV: value})

    @pytest.mark.parametrize("value", ["", "  ", "0", "false", "off", "no", "2", "enabled"])
    def test_these_leave_the_settings_choice(self, value: str):
        assert not air_gapped_by_environment({AIR_GAPPED_ENV: value})

    def test_an_unset_variable_leaves_the_settings_choice(self):
        assert not air_gapped_by_environment({})

    def test_the_real_environment_is_read_by_default(self, monkeypatch):
        monkeypatch.setenv(AIR_GAPPED_ENV, "on")
        assert air_gapped_by_environment()
        monkeypatch.delenv(AIR_GAPPED_ENV)
        assert not air_gapped_by_environment()


class TestHandingItOn:
    def test_an_air_gapped_process_sets_it_for_its_children(self):
        env: dict[str, str] = {}
        export_to(env, True)
        assert env == {AIR_GAPPED_ENV: "1"}
        assert air_gapped_by_environment(env)

    @pytest.mark.parametrize("standin", [1, "yes", object(), None])
    def test_a_stand_in_settings_value_does_not_switch_children_over(self, standin):
        env: dict[str, str] = {}
        export_to(env, standin)  # type: ignore[arg-type]
        assert env == {}

    def test_a_process_that_is_not_air_gapped_leaves_an_inherited_value(self):
        env = {AIR_GAPPED_ENV: "1"}
        export_to(env, False)
        assert env == {AIR_GAPPED_ENV: "1"}
