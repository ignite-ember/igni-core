"""Agno's run telemetry stays off, and only an explicit opt-in outside an air-gap turns it on.

``core/agno_telemetry.py``. Agno re-reads ``AGNO_TELEMETRY`` at every run
and posts to a third party when it reads ``true``, so igni's decision on
the variable is what a customer's network sees.
"""

from __future__ import annotations

import logging
import os

import pytest

from igni_core.core.agno_telemetry import AgnoTelemetry


class TestTheProcessDefault:
    def test_an_unset_variable_becomes_false(self):
        env: dict[str, str] = {}
        AgnoTelemetry.apply_process_default(env)
        assert env == {"AGNO_TELEMETRY": "false"}

    def test_an_explicit_true_is_honoured_outside_an_air_gap(self):
        env = {"AGNO_TELEMETRY": "true"}
        AgnoTelemetry.apply_process_default(env)
        assert env["AGNO_TELEMETRY"] == "true"

    @pytest.mark.parametrize("on", ["1", "true", "YES", " On "])
    def test_an_air_gap_from_the_environment_forces_an_explicit_true_off(self, on: str):
        env = {"AGNO_TELEMETRY": "true", "IGNI_AIR_GAPPED": on}
        AgnoTelemetry.apply_process_default(env)
        assert env["AGNO_TELEMETRY"] == "false"

    def test_it_acts_on_the_real_environment_when_none_is_given(self, monkeypatch):
        monkeypatch.delenv("AGNO_TELEMETRY", raising=False)
        monkeypatch.setenv("IGNI_AIR_GAPPED", "1")
        AgnoTelemetry.apply_process_default()
        assert os.environ["AGNO_TELEMETRY"] == "false"


class TestEnforce:
    def test_an_absent_variable_is_set_to_false(self):
        env: dict[str, str] = {}
        decision = AgnoTelemetry.enforce(env, air_gapped=False)
        assert env == {"AGNO_TELEMETRY": "false"}
        assert (decision.value, decision.overruled) == ("false", False)

    def test_an_air_gapped_true_is_overruled_and_says_so(self, caplog):
        env = {"AGNO_TELEMETRY": "TRUE"}
        with caplog.at_level(logging.WARNING, logger="igni_core.core.agno_telemetry"):
            decision = AgnoTelemetry.enforce(env, air_gapped=True)
        assert env["AGNO_TELEMETRY"] == "false"
        assert (decision.value, decision.overruled) == ("false", True)
        assert "air-gapped" in caplog.text and "os-api.agno.com" in caplog.text

    def test_an_explicit_true_outside_an_air_gap_is_left_alone(self):
        env = {"AGNO_TELEMETRY": "true"}
        decision = AgnoTelemetry.enforce(env, air_gapped=False)
        assert env["AGNO_TELEMETRY"] == "true"
        assert (decision.value, decision.overruled) == ("true", False)

    @pytest.mark.parametrize("standin", [1, "yes", object()])
    def test_a_truthy_stand_in_is_not_an_air_gap(self, standin):
        env = {"AGNO_TELEMETRY": "true"}
        decision = AgnoTelemetry.enforce(env, air_gapped=standin)  # type: ignore[arg-type]
        assert env["AGNO_TELEMETRY"] == "true" and not decision.overruled

    def test_a_value_agno_reads_as_off_is_not_overruled(self):
        env = {"AGNO_TELEMETRY": "1"}
        decision = AgnoTelemetry.enforce(env, air_gapped=True)
        assert (decision.value, decision.overruled) == ("1", False)


class TestIsOn:
    @pytest.mark.parametrize("value", ["true", "TRUE", " True ", "\ttrue\n"])
    def test_true_in_any_case_with_whitespace_is_on(self, value: str):
        assert AgnoTelemetry.is_on({"AGNO_TELEMETRY": value})

    @pytest.mark.parametrize("value", ["", "false", "1", "yes", "on", "truee"])
    def test_anything_else_is_off(self, value: str):
        assert not AgnoTelemetry.is_on({"AGNO_TELEMETRY": value})

    def test_an_absent_variable_is_off(self):
        assert not AgnoTelemetry.is_on({})

    def test_it_reads_the_real_environment_by_default(self, monkeypatch):
        monkeypatch.setenv("AGNO_TELEMETRY", "true")
        assert AgnoTelemetry.is_on()
        monkeypatch.setenv("AGNO_TELEMETRY", "false")
        assert not AgnoTelemetry.is_on()


class TestWhatIsHandedOn:
    def test_a_child_gets_the_process_s_own_value(self, monkeypatch):
        monkeypatch.setenv("AGNO_TELEMETRY", "true")
        assert AgnoTelemetry.child_environment() == {"AGNO_TELEMETRY": "true"}

    def test_a_child_gets_false_when_the_process_lost_the_variable(self, monkeypatch):
        monkeypatch.delenv("AGNO_TELEMETRY", raising=False)
        assert AgnoTelemetry.child_environment() == {"AGNO_TELEMETRY": "false"}

    def test_agents_and_teams_are_built_with_telemetry_off(self):
        assert AgnoTelemetry.CONSTRUCTOR_VALUE is False
