"""The payloads ToolEventHook fires, built directly: their keys, defaults, the
sanitising of arguments and results, and the extra fields they let through.

``test_hook_events_new.py`` reads the same keys off a real hook; this asks the
models themselves, so a default or a truncation limit cannot move unseen.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_core.core.hooks.payload_sanitizer import PayloadSanitizer
from igni_core.core.hooks.schemas import HookPayload
from igni_core.core.hooks.tool_events import (
    InstructionsLoadedPayload,
    PermissionDeniedPayload,
    PermissionRequestPayload,
    PostToolUseFailurePayload,
    PostToolUsePayload,
    PreToolUsePayload,
)

ARGS = {"n": 3, "d": {"a": 1}, "none": None, "long": "x" * 900}


def _wire(payload) -> dict:
    return HookPayload(body=payload).to_wire_dict()


def test_a_pre_tool_use_payload_carries_the_call_with_its_arguments_as_text():
    payload = PreToolUsePayload.from_call("grep", ARGS)

    assert _wire(payload) == {
        "session_id": "",
        "tool_name": "grep",
        "tool_args": {
            "n": "3",
            "d": "{'a': 1}",
            "none": "None",
            "long": "x" * PayloadSanitizer.MAX_ARG_CHARS,
        },
    }


def test_no_arguments_is_an_empty_mapping():
    assert PreToolUsePayload.from_call("grep", {}).tool_args == {}
    assert PermissionRequestPayload.from_call("grep", {}).tool_args == {}


def test_a_permission_request_payload_has_no_other_key():
    assert _wire(PermissionRequestPayload.from_call("grep", {"a": "b"})) == {
        "session_id": "",
        "tool_name": "grep",
        "tool_args": {"a": "b"},
    }


def test_a_post_tool_use_payload_carries_a_truncated_preview_of_the_result():
    payload = PostToolUsePayload.from_result("grep", {"a": 1}, "y" * 900)

    assert payload.tool_args == {"a": "1"}
    assert payload.result_preview == "y" * PayloadSanitizer.MAX_PREVIEW_CHARS


@pytest.mark.parametrize(
    ("result", "preview"),
    [(None, ""), (b"ab", "b'ab'"), ({"k": 1}, "{'k': 1}"), (7, "7"), ("", "")],
)
def test_a_result_of_any_kind_is_previewed_as_text(result, preview):
    assert PostToolUsePayload.from_result("t", {}, result).result_preview == preview


def test_a_failure_payload_carries_the_error_text():
    payload = PostToolUseFailurePayload.from_exception("grep", {"a": 1}, ValueError("boom é"))

    assert _wire(payload) == {
        "session_id": "",
        "tool_name": "grep",
        "tool_args": {"a": "1"},
        "error": "boom é",
    }


def test_an_exception_with_no_message_is_an_empty_error():
    assert PostToolUseFailurePayload.from_exception("t", {}, RuntimeError()).error == ""


def test_a_denied_payload_carries_the_stage_that_refused():
    payload = PermissionDeniedPayload.from_call("rm", {"p": "/"}, "permission_evaluator")

    assert _wire(payload) == {
        "session_id": "",
        "tool_name": "rm",
        "tool_args": {"p": "/"},
        "reason": "permission_evaluator",
    }


def test_an_instructions_loaded_payload_defaults_to_the_rules_index_with_nothing_loaded():
    assert _wire(InstructionsLoadedPayload()) == {
        "session_id": "",
        "source": "rules_index",
        "files": [],
        "bytes": 0,
    }


def test_a_payload_may_carry_a_field_ahead_of_its_model_and_it_reaches_the_wire():
    payload = PreToolUsePayload(tool_name="t", session_id="s", agent_id="a-1")

    assert _wire(payload)["agent_id"] == "a-1"
    assert _wire(payload)["session_id"] == "s"


def test_a_tool_payload_needs_a_tool_name():
    with pytest.raises(ValidationError):
        PreToolUsePayload()  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        PostToolUsePayload()  # type: ignore[call-arg]
