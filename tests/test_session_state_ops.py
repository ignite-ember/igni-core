"""Unit tests for ``session/state_ops.py``.

Extracted in iter 144. Session's own tests cover the delegate
methods; these tests pin the :class:`RuntimeModeCoordinator`
public surface in isolation so a future refactor can't silently
drop the hot-patch semantics (the whole point of ``state_ops``
is that both mutations take effect WITHOUT rebuilding the agent
— that's the invariant worth pinning).
"""

from types import SimpleNamespace

import pytest

from igni_core.core.config.permission_eval import PermissionEvaluator, PermissionMode
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
        assert payload == {"mode": "plan", "previous": "default"}


# Ensure the assertion above about ``pytest`` isn't accidentally unused.
def test_pytest_import_is_used():
    """Silences a linter that would flag pytest as unused since none of
    the tests above use it explicitly (async ones sometimes need it)."""
    assert pytest is not None
