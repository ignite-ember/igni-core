"""The failures that are expected come back as values.

Three of them used to be a string, a silent ``None``, or a swallowed
exception:

* the mode a session started in was read off the live evaluator, and a
  malformed configured mode was dropped without a word;
* an unknown output style came back as a string that happened to start
  with ``Error``;
* a group-policy revalidation that could not reach the portal was
  caught by whoever asked, with a bare ``except Exception``, and the
  reason never left that frame.

Each now carries its reason in a type the caller can read.
"""

from __future__ import annotations

import logging

import pytest

from igni_core.backend.cmd_session import SessionCommand
from igni_core.core.config.group_policy.revalidation import PolicyRevalidation
from igni_core.core.config.permission_eval import PermissionEvaluator, PermissionMode
from igni_core.core.config.schemas.permissions import PermissionsConfig
from igni_core.core.output_styles import OutputStyle
from igni_core.core.session.broadcast import BroadcastBus
from igni_core.core.session.output_style_selection import OutputStyleSelection
from igni_core.core.session.state_ops import RuntimeModeCoordinator, RuntimeModeDeps


def _styles() -> dict[str, OutputStyle]:
    return {
        "default": OutputStyle(name="default", body="Be terse.", description="", path=None),
        "explanatory": OutputStyle(
            name="explanatory", body="Explain a lot.", description="", path=None
        ),
    }


def _coordinator(
    *,
    evaluator: PermissionEvaluator | None = None,
    permissions: PermissionsConfig | None = None,
) -> RuntimeModeCoordinator:
    """The mode coordinator over collaborators a session would hand it."""
    live = evaluator if evaluator is not None else PermissionEvaluator.from_strings(mode="default")
    return RuntimeModeCoordinator(
        RuntimeModeDeps(
            evaluator_ref=lambda: live,
            output_style=OutputStyleSelection(_styles(), active="default"),
            team_ref=lambda: None,
            bus=BroadcastBus(),
            permissions=permissions,
        )
    )


class TestTheStartingModeIsAFact:
    """The mode a session starts in is a fact about its configuration.

    It used to be a reading of the live evaluator, taken the first time
    anyone asked — so it moved with the mode, and asking was itself a
    write.
    """

    def test_it_is_not_wherever_the_live_mode_happens_to_be(self) -> None:
        evaluator = PermissionEvaluator.from_strings(mode="acceptEdits")

        assert _coordinator(evaluator=evaluator).starting_mode is PermissionMode.DEFAULT

    def test_it_does_not_move_with_the_mode(self) -> None:
        """Flipping the mode does not move the starting mode."""
        evaluator = PermissionEvaluator.from_strings(mode="acceptEdits")
        coordinator = _coordinator(evaluator=evaluator)

        coordinator.set_permission_mode("plan")

        assert coordinator.starting_mode is PermissionMode.DEFAULT

    def test_the_configured_mode_is_the_starting_mode(self) -> None:
        """A configured mode is the one the session starts in."""
        permissions = PermissionsConfig.model_construct(mode="acceptEdits")

        coordinator = _coordinator(permissions=permissions)

        assert coordinator.starting_mode is PermissionMode.ACCEPT_EDITS


class TestAMalformedConfiguredModeIsNamed:
    def test_it_is_logged_and_the_session_starts_in_the_default(self, caplog) -> None:
        """A setting the user wrote and that never applies is named.

        The old code dropped it silently, and the session started in
        whatever mode the evaluator happened to report.
        """
        permissions = PermissionsConfig.model_construct(mode="bogus")
        coordinator = _coordinator(permissions=permissions)

        with caplog.at_level(logging.WARNING):
            starting = coordinator.starting_mode

        assert starting is PermissionMode.DEFAULT
        assert "bogus" in caplog.text


class TestTheOutputStyleRefusalIsTyped:
    def test_a_refusal_is_a_value_not_a_formatted_string(self) -> None:
        """The refusal says it was refused.

        It used to come back as a string that happened to start with
        ``Error``, so the caller had to read the first word to know
        whether the switch had happened.
        """
        switch = _coordinator().set_output_style("nonexistent")

        assert switch.refused is True
        assert switch.changed is False
        assert "nonexistent" in switch.text

    def test_a_switch_is_a_value_too(self) -> None:
        switch = _coordinator().set_output_style("explanatory")

        assert switch.refused is False
        assert switch.changed is True


class _ClearSession:
    """A session-shaped stub for the ``/clear`` revalidation path."""

    def __init__(self, outcome: PolicyRevalidation) -> None:
        async def _hook() -> PolicyRevalidation:
            return outcome

        self.on_new_dialogue = _hook


class TestTheClearPathReportsTheFailure:
    """``/clear`` starts a new dialogue without the session-start RPC.

    It revalidates the group in the background, so the failure has
    nowhere to go but the log — and it used to go nowhere at all.
    """

    @pytest.mark.asyncio
    async def test_a_failed_revalidation_is_logged(self, caplog) -> None:
        session = _ClearSession(PolicyRevalidation(error="portal unreachable"))

        with caplog.at_level(logging.WARNING):
            await SessionCommand(session)._revalidate_group()

        assert "portal unreachable" in caplog.text

    @pytest.mark.asyncio
    async def test_a_successful_revalidation_says_nothing(self, caplog) -> None:
        session = _ClearSession(PolicyRevalidation(changed=True))

        with caplog.at_level(logging.WARNING):
            await SessionCommand(session)._revalidate_group()

        assert caplog.text == ""
