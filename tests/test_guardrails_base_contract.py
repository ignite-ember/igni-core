"""The guardrail base class: its result model, its registry and its gate keys."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import igni_core.core.guardrails as guardrails_pkg
from igni_core.core.guardrails.base import Guardrail, GuardrailResult
from igni_core.core.guardrails.injection import PromptInjectionGuardrail
from igni_core.core.guardrails.moderation import ModerationGuardrail
from igni_core.core.guardrails.pii import PIIGuardrail


class TestGuardrailResult:
    def test_findings_default_to_an_empty_list_per_instance(self):
        a = GuardrailResult(passed=True, guardrail="g", message="m")
        b = GuardrailResult(passed=True, guardrail="g", message="m")
        a.findings.append("x")

        assert b.findings == []

    @pytest.mark.parametrize("missing", ["passed", "guardrail", "message"])
    def test_a_result_without_a_required_field_is_refused(self, missing):
        fields = {"passed": True, "guardrail": "g", "message": "m"}
        del fields[missing]

        with pytest.raises(ValidationError):
            GuardrailResult(**fields)


class TestTheBaseCheck:
    def test_it_passes_under_its_own_name_with_nothing_to_say(self):
        result = Guardrail().check("anything at all")

        assert result == GuardrailResult(passed=True, guardrail="base", message="", findings=[])

    def test_a_subclass_reports_under_its_own_name(self):
        class Named(Guardrail):
            name = "named_one"
            gate_key = "named_one_flag"

        assert Named().check("x").guardrail == "named_one"


class TestTheGateKeyIsRequiredAtDefinition:
    def test_a_subclass_without_one_is_refused_and_named(self):
        with pytest.raises(TypeError, match="NoKeyGuardrail") as err:

            class NoKeyGuardrail(Guardrail):
                pass

        assert "gate_key" in str(err.value)

    def test_an_empty_key_is_refused_too(self):
        with pytest.raises(TypeError, match="EmptyKeyGuardrail"):

            class EmptyKeyGuardrail(Guardrail):
                gate_key = ""

    def test_an_abstract_subclass_may_omit_it(self):
        class AbstractOne(Guardrail):
            _abstract_guardrail = True

        assert AbstractOne.gate_key == ""

    def test_a_subclass_inherits_the_key_of_its_parent(self):
        class Parent(Guardrail):
            gate_key = "inherit_parent_flag"

        class Child(Parent):
            pass

        assert Child.gate_key == "inherit_parent_flag"

    def test_a_subclass_of_an_abstract_one_still_needs_its_own(self):
        class Abstract(Guardrail):
            _abstract_guardrail = True

        with pytest.raises(TypeError, match="Leaf"):

            class Leaf(Abstract):
                pass


class TestIterEnabled:
    def test_it_yields_only_guardrails_whose_flag_is_truthy(self):
        class On(Guardrail):
            gate_key = "iter_on_flag"

        class Off(Guardrail):
            gate_key = "iter_off_flag"

        class Missing(Guardrail):
            gate_key = "iter_missing_flag"

        cfg = SimpleNamespace(iter_on_flag=True, iter_off_flag=False)
        got = [type(g) for g in Guardrail.iter_enabled(cfg)]

        assert On in got
        assert Off not in got
        assert Missing not in got

    def test_it_yields_a_fresh_instance_each_time(self):
        class Fresh(Guardrail):
            gate_key = "iter_fresh_flag"

        cfg = SimpleNamespace(iter_fresh_flag=True)
        first = next(g for g in Guardrail.iter_enabled(cfg) if isinstance(g, Fresh))
        second = next(g for g in Guardrail.iter_enabled(cfg) if isinstance(g, Fresh))

        assert first is not second

    def test_it_finds_a_grandchild_and_skips_an_abstract_parent(self):
        class Middle(Guardrail):
            _abstract_guardrail = True

        class Grand(Middle):
            gate_key = "iter_grand_flag"

        cfg = SimpleNamespace(iter_grand_flag=True)
        got = [type(g) for g in Guardrail.iter_enabled(cfg)]

        assert Grand in got
        assert Middle not in got

    def test_a_grandchild_of_a_concrete_guardrail_is_found(self):
        class Strict(PIIGuardrail):
            gate_key = "iter_strict_flag"

        cfg = SimpleNamespace(iter_strict_flag=True)

        assert [type(g) for g in Guardrail.iter_enabled(cfg)] == [Strict]

    def test_a_truthy_non_bool_flag_enables_it(self):
        class Loose(Guardrail):
            gate_key = "iter_loose_flag"

        cfg = SimpleNamespace(iter_loose_flag="yes")

        assert Loose in [type(g) for g in Guardrail.iter_enabled(cfg)]

    def test_all_subclasses_walks_depth_first(self):
        class Root(Guardrail):
            _abstract_guardrail = True

        class A(Root):
            gate_key = "df_a"

        class A1(A):
            pass

        class B(Root):
            gate_key = "df_b"

        assert list(Root._all_subclasses()) == [A, A1, B]


class TestThePackageRegistry:
    def test_importing_the_package_registers_every_concrete_guardrail(self):
        registered = set(Guardrail._all_subclasses())

        assert {PIIGuardrail, PromptInjectionGuardrail, ModerationGuardrail} <= registered

    def test_the_gate_keys_are_the_config_fields(self):
        assert PIIGuardrail.gate_key == "pii_detection"
        assert PromptInjectionGuardrail.gate_key == "prompt_injection"
        assert ModerationGuardrail.gate_key == "moderation"

    def test_the_package_exports(self):
        assert sorted(guardrails_pkg.__all__) == [
            "Guardrail",
            "GuardrailResult",
            "GuardrailRunner",
            "ModerationGuardrail",
            "PIIGuardrail",
            "PromptInjectionGuardrail",
        ]
        for name in guardrails_pkg.__all__:
            assert getattr(guardrails_pkg, name) is not None
