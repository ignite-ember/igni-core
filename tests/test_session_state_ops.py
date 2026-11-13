"""Unit tests for ``session/state_ops.py``.

Extracted in iter 144. Session's own tests cover the delegate
methods; these tests pin the :class:`RuntimeModeCoordinator`
public surface in isolation so a future refactor can't silently
drop the hot-patch semantics (the whole point of ``state_ops``
is that both mutations take effect WITHOUT rebuilding the agent
— that's the invariant worth pinning).
"""

import logging
from types import SimpleNamespace

import pytest

from igni_core.core.config.permission_eval import PermissionEvaluator, PermissionMode
from igni_core.core.config.permission_eval.mode_policy import ModePolicy
from igni_core.core.config.schemas.permissions import PermissionsConfig
from igni_core.core.output_styles import OutputStyle
from igni_core.core.session.broadcast import BroadcastBus
from igni_core.core.session.output_style_selection import OutputStyleSelection
from igni_core.core.session.state_ops import RuntimeModeCoordinator, RuntimeModeDeps


def _bare_session(styles: dict[str, OutputStyle] | None = None):
    """Session-shaped stub carrying only what state_ops reads."""
    default_styles = (
        styles
        if styles is not None
        else {
            "default": OutputStyle(name="default", body="Be terse.", description="", path=None),
            "explanatory": OutputStyle(
                name="explanatory", body="Explain a lot.", description="", path=None
            ),
        }
    )
    return SimpleNamespace(
        output_style=OutputStyleSelection(default_styles, active="default"),
        main_team=SimpleNamespace(instructions=[]),
        broadcast_bus=BroadcastBus(),
        permission_evaluator=PermissionEvaluator.from_strings(mode="default"),
    )


def _coord(s) -> RuntimeModeCoordinator:
    """The coordinator over ``s``'s collaborators, as the session builds it."""
    return RuntimeModeCoordinator(
        RuntimeModeDeps(
            evaluator_ref=lambda: s.permission_evaluator,
            output_style=s.output_style,
            team_ref=lambda: s.main_team,
            bus=s.broadcast_bus,
        )
    )


class TestSetOutputStyle:
    def test_switches_active_style(self):
        s = _bare_session()
        switch = _coord(s).set_output_style("explanatory")
        assert s.output_style.active == "explanatory"
        assert switch.refused is False
        assert switch.changed is True
        assert "default" in switch.text and "explanatory" in switch.text

    def test_unknown_style_returns_error(self):
        s = _bare_session()
        switch = _coord(s).set_output_style("nonexistent")
        assert switch.refused is True
        assert switch.changed is False
        assert "Error" in switch.text
        # Unchanged state.
        assert s.output_style.active == "default"
        # Available styles listed for discoverability.
        assert "default" in switch.text
        assert "explanatory" in switch.text

    def test_hot_patches_team_instructions(self):
        # Headline invariant: the switch mutates the LIVE team's
        # instructions list; no rebuild. Next ``arun`` sees the
        # new style without a team constructor call.
        s = _bare_session()
        s.main_team.instructions = [
            "system prompt prefix",
            "# Output style: default\n\nBe terse.",
            "trailing block",
        ]
        _coord(s).set_output_style("explanatory")
        # Old style block replaced, not appended alongside.
        assert not any(
            isinstance(i, str) and i.startswith("# Output style: default")
            for i in s.main_team.instructions
        )
        assert any(
            isinstance(i, str) and i.startswith("# Output style: explanatory")
            for i in s.main_team.instructions
        )

    def test_empty_body_style_prunes_block(self):
        # A style with empty body should REMOVE the existing block
        # rather than append a header-only entry.
        s = _bare_session(
            styles={
                "default": OutputStyle(name="default", body="Be terse.", description="", path=None),
                "silent": OutputStyle(name="silent", body="", description="", path=None),
            }
        )
        s.main_team.instructions = ["# Output style: default\n\nBe terse."]
        _coord(s).set_output_style("silent")
        # No # Output style: header should remain.
        assert not any(
            isinstance(i, str) and i.startswith("# Output style:") for i in s.main_team.instructions
        )

    def test_broadcasts_change(self):
        s = _bare_session()
        captured: list[tuple[str, dict]] = []
        s.broadcast_bus.register(lambda e: captured.append((e.channel, e.to_payload())))
        _coord(s).set_output_style("explanatory")
        assert len(captured) == 1
        channel, payload = captured[0]
        assert channel == "output_style_changed"
        assert payload == {"style": "explanatory", "previous": "default"}

    def test_switch_to_same_style_reports_noop(self):
        s = _bare_session()
        switch = _coord(s).set_output_style("default")
        assert switch.refused is False
        assert switch.changed is False
        assert "already" in switch.text.lower()

    def test_no_team_yet_is_ok(self):
        # No main team built yet.
        s = _bare_session()
        s.main_team = None
        switch = _coord(s).set_output_style("explanatory")
        # Style change still recorded + broadcast.
        assert s.output_style.active == "explanatory"
        assert "explanatory" in switch.text


class TestSetPermissionMode:
    def test_flips_mode(self):
        s = _bare_session()
        msg = _coord(s).set_permission_mode("plan").text
        assert s.permission_evaluator.mode is PermissionMode.PLAN
        assert "plan" in msg

    def test_unknown_mode_returns_error(self):
        s = _bare_session()
        msg = _coord(s).set_permission_mode("invalid_mode").text
        assert "Error" in msg
        # Valid modes listed for discoverability.
        assert "default" in msg
        # Unchanged state.
        assert s.permission_evaluator.mode is PermissionMode.DEFAULT

    def test_same_mode_reports_noop(self):
        s = _bare_session()
        captured: list[tuple[str, dict]] = []
        s.broadcast_bus.register(lambda e: captured.append((e.channel, e.to_payload())))
        msg = _coord(s).set_permission_mode("default").text
        assert "already" in msg.lower()
        assert captured == []

    def test_broadcasts_change(self):
        s = _bare_session()
        captured: list[tuple[str, dict]] = []
        s.broadcast_bus.register(lambda e: captured.append((e.channel, e.to_payload())))
        _coord(s).set_permission_mode("plan")
        assert len(captured) == 1
        channel, payload = captured[0]
        assert channel == "permission_mode_changed"
        assert payload == {"kind": "switched", "mode": "plan", "previous": "default"}


def _coord_with(s, permissions) -> RuntimeModeCoordinator:
    return RuntimeModeCoordinator(
        RuntimeModeDeps(
            evaluator_ref=lambda: s.permission_evaluator,
            output_style=s.output_style,
            team_ref=lambda: s.main_team,
            bus=s.broadcast_bus,
            permissions=permissions,
        )
    )


def _events(s) -> list[tuple[str, dict]]:
    captured: list[tuple[str, dict]] = []
    s.broadcast_bus.register(lambda e: captured.append((e.channel, e.to_payload())))
    return captured


class TestTheStyleSwitchEdges:
    @pytest.mark.parametrize("name", ["", "   ", None])
    def test_a_blank_or_missing_name_is_refused_as_an_empty_style(self, name):
        s = _bare_session()

        switch = _coord(s).set_output_style(name)  # type: ignore[arg-type]

        assert switch.refused is True and switch.changed is False
        assert "unknown output style ''" in switch.text
        assert s.output_style.active == "default"

    def test_the_name_is_stripped_before_it_is_looked_up(self):
        s = _bare_session()

        switch = _coord(s).set_output_style("  explanatory \n")

        assert switch.refused is False and switch.changed is True
        assert s.output_style.active == "explanatory"

    def test_the_refusal_lists_the_available_styles_sorted(self):
        s = _bare_session(
            styles={
                n: OutputStyle(name=n, body="b", description="", path=None)
                for n in ("zeta", "alpha", "mid")
            }
        )

        switch = _coord(s).set_output_style("nope")

        assert switch.text == ("Error: unknown output style 'nope'. Available: alpha, mid, zeta")

    def test_with_no_styles_the_refusal_says_none_are_configured(self):
        s = _bare_session(styles={})

        switch = _coord(s).set_output_style("anything")

        assert switch.text == "Error: unknown output style 'anything'. Available: (none configured)"

    def test_choosing_the_active_style_again_still_broadcasts_it_with_itself_as_previous(self):
        s = _bare_session()
        seen = _events(s)

        switch = _coord(s).set_output_style("default")

        assert switch.text == "Output style already default."
        assert seen == [("output_style_changed", {"style": "default", "previous": "default"})]

    def test_a_refused_style_broadcasts_nothing(self):
        s = _bare_session()
        seen = _events(s)

        _coord(s).set_output_style("nope")

        assert seen == []

    def test_the_status_line_names_both_styles(self):
        s = _bare_session()

        switch = _coord(s).set_output_style("explanatory")

        assert switch.text == "Output style: default \u2192 explanatory."

    def test_a_style_with_a_unicode_name_is_switched_to(self):
        s = _bare_session(
            styles={
                "default": OutputStyle(name="default", body="x", description="", path=None),
                "stil-\u00fc": OutputStyle(name="stil-\u00fc", body="y", description="", path=None),
            }
        )

        _coord(s).set_output_style("stil-\u00fc")

        assert s.main_team.instructions == ["# Output style: stil-\u00fc\n\ny"]


class TestTheInstructionsPatcher:
    def test_it_keeps_what_is_not_the_style_block_and_prunes_a_look_alike_user_line(self):
        s = _bare_session()
        s.main_team.instructions = [
            "keep me",
            42,
            "# Output style: default\n\nold",
            "# Output style:glued marker",
            "  # Output style: indented is not a block",
        ]

        _coord(s).set_output_style("explanatory")

        assert s.main_team.instructions == [
            "keep me",
            42,
            "  # Output style: indented is not a block",
            "# Output style: explanatory\n\nExplain a lot.",
        ]

    def test_instructions_that_are_not_a_list_are_left_alone(self):
        s = _bare_session()
        s.main_team.instructions = "one string prompt"

        _coord(s).set_output_style("explanatory")

        assert s.main_team.instructions == "one string prompt"

    def test_the_team_replaced_since_the_last_switch_is_the_one_patched(self):
        s = _bare_session()
        coordinator = _coord(s)
        coordinator.set_output_style("explanatory")
        s.main_team = SimpleNamespace(instructions=["fresh team"])

        coordinator.set_output_style("default")

        assert s.main_team.instructions == ["fresh team", "# Output style: default\n\nBe terse."]


class TestTheStartingMode:
    def test_without_permissions_it_is_the_default_and_the_policy_is_empty(self):
        coordinator = _coord(_bare_session())

        assert coordinator.starting_mode is PermissionMode.DEFAULT
        assert coordinator.policy == ModePolicy()

    def test_it_is_the_configured_mode(self):
        permissions = PermissionsConfig(mode=PermissionMode.ACCEPT_EDITS)

        coordinator = _coord_with(_bare_session(), permissions)

        assert coordinator.starting_mode is PermissionMode.ACCEPT_EDITS

    def test_it_is_fixed_when_the_mode_changes_later(self):
        s = _bare_session()
        coordinator = _coord_with(s, PermissionsConfig(mode=PermissionMode.ACCEPT_EDITS))

        coordinator.set_permission_mode("plan")

        assert coordinator.starting_mode is PermissionMode.ACCEPT_EDITS

    def test_a_configured_mode_that_is_not_a_mode_is_named_in_a_warning_and_defaults(self, caplog):
        permissions = PermissionsConfig.model_construct(mode="yolo")

        with caplog.at_level(logging.WARNING, logger="igni_core.core.session.state_ops"):
            coordinator = _coord_with(_bare_session(), permissions)

        assert coordinator.starting_mode is PermissionMode.DEFAULT
        (record,) = caplog.records
        assert "'yolo'" in record.getMessage() and "default" in record.getMessage()


class TestTheOrganisationsPolicy:
    def _policy_permissions(self, policy) -> PermissionsConfig:
        permissions = PermissionsConfig()
        permissions.set_mode_policy(policy)
        return permissions

    def test_the_recorded_policy_is_exposed(self):
        policy = ModePolicy(allow_bypass=False, bypass_source=ModePolicy.MANAGED)

        coordinator = _coord_with(_bare_session(), self._policy_permissions(policy))

        assert coordinator.policy is policy

    def test_a_policy_that_is_not_a_mode_policy_is_the_empty_one(self):
        coordinator = _coord_with(_bare_session(), self._policy_permissions("deny everything"))

        assert coordinator.policy == ModePolicy()

    def test_a_refused_bypass_changes_nothing_and_broadcasts_nothing(self):
        s = _bare_session()
        policy = ModePolicy(allow_bypass=False, bypass_source=ModePolicy.MANAGED)
        seen = _events(s)

        switch = _coord_with(s, self._policy_permissions(policy)).set_permission_mode(
            PermissionMode.BYPASS_PERMISSIONS.value
        )

        assert switch.refused is True and switch.changed is False
        assert switch.text == policy.refusal(PermissionMode.BYPASS_PERMISSIONS)
        assert "managed settings" in switch.text
        assert s.permission_evaluator.mode is PermissionMode.DEFAULT
        assert seen == []

    def test_a_mode_looser_than_the_floor_is_refused_and_a_stricter_one_is_not(self):
        s = _bare_session()
        policy = ModePolicy.from_tiers({"permissions": {"mode": "default"}}, None)
        coordinator = _coord_with(s, self._policy_permissions(policy))

        refused = coordinator.set_permission_mode("acceptEdits")
        allowed = coordinator.set_permission_mode("plan")

        assert refused.refused is True and "managed settings" in refused.text
        assert "looser than the default mode" in refused.text
        assert allowed.refused is False and allowed.changed is True
        assert s.permission_evaluator.mode is PermissionMode.PLAN


class TestThePermissionSwitchEdges:
    def test_the_status_line_names_both_modes(self):
        switch = _coord(_bare_session()).set_permission_mode("plan")

        assert switch.text == "Permission mode: default \u2192 plan."
        assert switch.changed is True and switch.refused is False

    def test_the_unknown_mode_refusal_lists_every_valid_mode(self):
        switch = _coord(_bare_session()).set_permission_mode("yolo")

        assert switch.refused is True
        expected = ", ".join(m.value for m in PermissionMode)
        assert switch.text == f"Error: unknown permission mode 'yolo'. Valid: {expected}"

    def test_the_evaluator_is_read_at_each_call_so_a_reloaded_one_is_the_one_flipped(self):
        s = _bare_session()
        coordinator = _coord(s)
        coordinator.set_permission_mode("plan")
        replacement = PermissionEvaluator.from_strings(mode="default")
        s.permission_evaluator = replacement

        switch = coordinator.set_permission_mode("plan")

        assert switch.changed is True
        assert replacement.mode is PermissionMode.PLAN


# Ensure the assertion above about ``pytest`` isn't accidentally unused.
def test_pytest_import_is_used():
    """Silences a linter that would flag pytest as unused since none of
    the tests above use it explicitly (async ones sometimes need it)."""
    assert pytest is not None
