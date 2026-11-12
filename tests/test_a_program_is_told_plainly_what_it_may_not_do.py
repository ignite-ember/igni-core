"""What :func:`standing_policy.refusal` refuses, and the words it says it in.

The earlier tests pin each window-only call and the approver's once-rule
on single rows. These pin the edges: a batch refused for any one of its
rows, an empty choice counting as once, every window-only command, a
command line with no usable name, the ``shutdown`` message, a message
that carries no standing at all, and the plain reason a program is given.
"""

from __future__ import annotations

import pytest

from igni_core.backend import standing_policy
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod
from igni_core.protocol.schemas.envelope import Standing


def _from(standing: Standing, message: msg.Message) -> msg.Message:
    message.mark_from("ws-1", standing)
    return message


def _row(**fields: str) -> msg.HITLDecision:
    return msg.HITLDecision(requirement_id="r", action="confirm", **fields)


def test_a_batch_is_refused_for_one_bad_row_after_good_ones() -> None:
    batch = msg.HITLResponseBatch(
        decisions=[_row(choice="once"), _row(choice=""), _row(choice="always")]
    )

    reason = standing_policy.refusal(_from(Standing.APPROVER, batch))

    assert (
        reason == "hitl_response_batch: an approver answers once; only igni's window saves a rule"
    )
    assert standing_policy.refusal(_from(Standing.WINDOW, batch)) is None


def test_a_batch_is_refused_for_a_mode_change_in_a_later_row() -> None:
    batch = msg.HITLResponseBatch(
        decisions=[_row(choice="once"), _row(choice="once", set_permission_mode="acceptEdits")]
    )

    reason = standing_policy.refusal(_from(Standing.APPROVER, batch))

    assert reason is not None and "changes the permission mode" in reason


def test_an_empty_choice_and_an_empty_batch_count_as_once() -> None:
    assert (
        standing_policy.refusal(
            _from(Standing.APPROVER, msg.HITLResponseBatch(decisions=[_row(choice="")]))
        )
        is None
    )
    assert (
        standing_policy.refusal(_from(Standing.APPROVER, msg.HITLResponseBatch(decisions=[])))
        is None
    )


def test_a_program_is_told_it_needs_an_approver_token() -> None:
    reason = standing_policy.refusal(
        _from(Standing.PROGRAM, msg.HITLResponse(requirement_id="r", action="confirm"))
    )

    assert reason is not None
    assert reason.startswith("hitl_response: a program answers approval requests only with")
    assert "approver token" in reason


@pytest.mark.parametrize("command", sorted(standing_policy.WINDOW_ONLY_COMMANDS))
def test_every_window_only_command_is_refused_to_a_program(command: str) -> None:
    for standing in (Standing.PROGRAM, Standing.APPROVER):
        reason = standing_policy.refusal(_from(standing, msg.Command(text=command)))
        assert reason == f"{command}: only igni's window may do this"
    assert standing_policy.refusal(_from(Standing.WINDOW, msg.Command(text=command))) is None


def test_the_window_only_commands_are_the_ones_that_loosen_or_sign_in() -> None:
    expected = {"/bypass", "/accept", "/plugin", "/eject", "/login", "/logout"}
    assert expected == standing_policy.WINDOW_ONLY_COMMANDS


@pytest.mark.parametrize("text", ["", "   ", "\n", "/", "/ plugin"])
def test_a_command_line_with_no_usable_name_is_allowed(text: str) -> None:
    assert standing_policy.refusal(_from(Standing.PROGRAM, msg.Command(text=text))) is None


@pytest.mark.parametrize("text", ["  /LOGIN  now", "login", "\t/Logout"])
def test_a_command_is_matched_by_its_first_word_ignoring_case_and_the_slash(text: str) -> None:
    assert standing_policy.refusal(_from(Standing.PROGRAM, msg.Command(text=text)))


def test_a_program_cannot_stop_igni_with_the_shutdown_message() -> None:
    assert (
        standing_policy.refusal(_from(Standing.PROGRAM, msg.Shutdown()))
        == "shutdown: only igni's window may stop igni"
    )
    assert standing_policy.refusal(_from(Standing.APPROVER, msg.Shutdown()))
    assert standing_policy.refusal(_from(Standing.WINDOW, msg.Shutdown())) is None


def test_a_message_that_carries_no_standing_is_the_window_s() -> None:
    assert standing_policy.refusal(msg.Shutdown()) is None
    assert standing_policy.refusal(object()) is None


def test_a_call_with_a_caller_is_refused_even_when_the_method_is_open() -> None:
    request = msg.RPCRequest(id="x", method=RpcMethod.GET_STATUS, caller="acme.reviewer")

    reason = standing_policy.refusal(_from(Standing.PROGRAM, request))

    assert reason == f"{RpcMethod.GET_STATUS}: only igni's window speaks for an extension"


def test_a_window_only_call_is_refused_in_plain_words() -> None:
    reason = standing_policy.refusal(
        _from(Standing.PROGRAM, msg.RPCRequest(id="x", method=RpcMethod.RUN_SHELL))
    )

    assert reason == f"{RpcMethod.RUN_SHELL}: only igni's window may do this"


def test_an_unknown_method_from_a_program_is_allowed() -> None:
    request = msg.RPCRequest(id="x", method="some.method.nobody.listed")

    assert standing_policy.refusal(_from(Standing.PROGRAM, request)) is None
