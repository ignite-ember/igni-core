"""The run pipeline's models: which phases hold a run, the hook payload shapes,
extra keys kept, and the cancel and gate results."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_core.backend import schemas_run
from igni_core.backend.schemas_run import (
    CancelAgentRunResult,
    CancelRunResult,
    HookGateResult,
    HttpClientCloseResult,
    ModelConfig,
    PromptBuildResult,
    RunPhase,
    StopHookPayload,
    UserPromptSubmitPayload,
)
from igni_core.core.config.model_entry import ModelRegistryEntry
from igni_core.protocol import messages as msg


@pytest.mark.parametrize(
    ("phase", "active"),
    [
        (RunPhase.idle, False),
        (RunPhase.starting, True),
        (RunPhase.streaming, True),
        (RunPhase.finalizing, True),
        (RunPhase.errored, False),
        (RunPhase.done, False),
    ],
)
def test_only_starting_streaming_and_finalizing_hold_a_run(phase, active):
    assert phase.is_active() is active


def test_the_six_phases_are_named_by_their_words():
    assert [p.value for p in RunPhase] == [
        "idle",
        "starting",
        "streaming",
        "finalizing",
        "errored",
        "done",
    ]


def test_the_hook_payloads_dump_exactly_what_a_hook_is_handed():
    assert UserPromptSubmitPayload(message="hi", session_id="s").model_dump() == {
        "message": "hi",
        "session_id": "s",
    }
    assert StopHookPayload(session_id="s").model_dump() == {"session_id": "s"}


def test_a_model_config_with_no_fields_is_text_only_and_nameless():
    config = ModelConfig()
    assert (config.vision, config.model_id) == (False, "")


def test_a_model_config_keeps_the_keys_a_provider_added():
    entry = ModelRegistryEntry(model_id="m", vision=True, source="cloud", wire_api="chat")
    config = ModelConfig.model_validate(entry.model_dump())
    assert config.vision is True
    assert config.model_id == "m"
    dumped = config.model_dump()
    assert dumped["source"] == "cloud"
    assert dumped["wire_api"] == "chat"
    assert dumped["provider"] == "openai_like"


def test_a_closed_client_says_why_only_when_it_failed():
    assert HttpClientCloseResult(ok=True).reason == ""
    failed = HttpClientCloseResult(ok=False, reason="boom")
    assert (failed.ok, failed.reason) == (False, "boom")


def test_a_cancel_says_whether_a_turn_was_running():
    assert CancelRunResult(cancelled=True).model_dump() == {"cancelled": True}
    assert CancelRunResult(cancelled=False).model_dump() == {"cancelled": False}


def test_an_agent_run_cancel_names_its_error_only_on_failure():
    assert CancelAgentRunResult(ok=True).model_dump() == {"ok": True, "error": ""}
    unknown = CancelAgentRunResult(ok=False, error="unknown run id")
    assert unknown.model_dump() == {"ok": False, "error": "unknown run id"}


def test_a_gate_result_defaults_to_no_message_context_or_text():
    gate = HookGateResult(should_continue=True)
    assert (gate.block_message, gate.context_message, gate.text) == (None, None, None)
    blocked = HookGateResult(should_continue=False)
    assert blocked.should_continue is False and blocked.block_message is None


def test_a_gate_result_carries_the_text_an_extension_rewrote():
    gate = HookGateResult(should_continue=True, text="rewritten")
    assert gate.text == "rewritten"


def test_a_gate_result_must_say_whether_to_continue():
    with pytest.raises(ValidationError):
        HookGateResult()


def test_a_prompt_build_result_may_have_no_media_and_no_announcements():
    built = PromptBuildResult(message="m", media=None)
    assert (built.message, built.media, built.info_messages) == ("m", None, [])


def test_a_prompt_build_result_keeps_its_announcements_in_order():
    infos = [msg.Info(text="Referenced: a"), msg.Info(text="Attached 1 URL(s)")]
    built = PromptBuildResult(message="m", media=None, info_messages=infos)
    assert [i.text for i in built.info_messages] == ["Referenced: a", "Attached 1 URL(s)"]


def test_every_public_result_is_exported():
    public = {
        name
        for name, value in vars(schemas_run).items()
        if isinstance(value, type)
        and value.__module__ == schemas_run.__name__
        and not name.startswith("_")
    }
    assert public <= set(schemas_run.__all__)
