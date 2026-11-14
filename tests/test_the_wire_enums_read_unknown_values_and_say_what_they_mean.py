"""The protocol's enums: which values they hold, what an unreadable one becomes, what each predicate says."""

from __future__ import annotations

import pytest

from igni_core.protocol.schemas.enums import (
    CommandAction,
    CommandResultKind,
    HITLAction,
    HITLChoice,
    OrchestrationTaskStatus,
    PermissionModeName,
    PushChannel,
    SchedulerEventType,
)

INGEST_ENUMS = [
    OrchestrationTaskStatus,
    HITLAction,
    HITLChoice,
    SchedulerEventType,
    PermissionModeName,
    PushChannel,
]

UNREADABLE = ["", "Confirm", "ONCE", "dontask", "nonsense", None, 7, 1.5, b"once"]


@pytest.mark.parametrize("enum", INGEST_ENUMS, ids=lambda e: e.__name__)
@pytest.mark.parametrize("value", UNREADABLE, ids=repr)
def test_an_unreadable_value_becomes_unknown_instead_of_raising(enum, value):
    assert enum(value) is enum.UNKNOWN


@pytest.mark.parametrize("enum", INGEST_ENUMS, ids=lambda e: e.__name__)
def test_every_listed_value_reads_back_as_its_member(enum):
    for member in enum:
        assert enum(member.value) is member


@pytest.mark.parametrize("enum", INGEST_ENUMS, ids=lambda e: e.__name__)
def test_unknown_is_the_word_unknown(enum):
    assert enum.UNKNOWN.value == "unknown"


@pytest.mark.parametrize("enum", [CommandResultKind, CommandAction], ids=lambda e: e.__name__)
@pytest.mark.parametrize("value", ["nonsense", "Quit", "MARKDOWN", None, 3])
def test_a_command_value_not_listed_raises(enum, value):
    with pytest.raises(ValueError):
        enum(value)


def test_command_result_kinds():
    assert [k.value for k in CommandResultKind] == ["markdown", "info", "error", "action"]


def test_command_actions_are_exactly_these_and_none_is_the_empty_string():
    assert CommandAction("") is CommandAction.NONE
    assert {a.value for a in CommandAction} == {
        "",
        "quit",
        "clear",
        "fork",
        "sessions",
        "model",
        "model_switched",
        "login",
        "logout",
        "help",
        "mcp",
        "plugins",
        "extensions",
        "agents",
        "skills",
        "hooks",
        "loop",
        "schedule",
        "watcher",
        "compact",
        "ctx",
        "programs",
        "run_prompt",
    }


@pytest.mark.parametrize(
    ("status", "terminal", "active"),
    [
        (OrchestrationTaskStatus.PENDING, False, True),
        (OrchestrationTaskStatus.RUNNING, False, True),
        (OrchestrationTaskStatus.COMPLETED, True, False),
        (OrchestrationTaskStatus.FAILED, True, False),
        (OrchestrationTaskStatus.CANCELLED, True, False),
        (OrchestrationTaskStatus.UNKNOWN, False, False),
    ],
)
def test_a_task_status_is_terminal_or_active_and_unknown_is_neither(status, terminal, active):
    assert status.is_terminal() is terminal
    assert status.is_active() is active


@pytest.mark.parametrize(
    ("action", "confirms"),
    [(HITLAction.CONFIRM, True), (HITLAction.REJECT, False), (HITLAction.UNKNOWN, False)],
)
def test_only_confirm_approves(action, confirms):
    assert action.is_confirm() is confirms
    assert HITLAction("approve").is_confirm() is False


@pytest.mark.parametrize(
    ("choice", "persistent"),
    [
        (HITLChoice.ONCE, False),
        (HITLChoice.ALWAYS, True),
        (HITLChoice.SIMILAR, True),
        (HITLChoice.DENY, True),
        (HITLChoice.UNKNOWN, False),
    ],
)
def test_a_choice_is_saved_as_a_rule_unless_it_is_once_or_unreadable(choice, persistent):
    assert choice.is_persistent() is persistent


@pytest.mark.parametrize(
    ("event", "terminal"),
    [
        (SchedulerEventType.STARTED, False),
        (SchedulerEventType.COMPLETED, True),
        (SchedulerEventType.FAILED, True),
        (SchedulerEventType.ERROR, True),
        (SchedulerEventType.UNKNOWN, False),
    ],
)
def test_a_scheduler_event_is_terminal_once_it_completed_or_failed(event, terminal):
    assert event.is_terminal() is terminal


def test_error_is_a_value_of_its_own_beside_failed():
    assert SchedulerEventType("error") is SchedulerEventType.ERROR
    assert SchedulerEventType.ERROR is not SchedulerEventType.FAILED


def test_the_permission_modes_keep_their_camel_case_wire_values():
    assert [m.value for m in PermissionModeName if m is not PermissionModeName.UNKNOWN] == [
        "default",
        "dontAsk",
        "acceptEdits",
        "bypassPermissions",
        "plan",
    ]
    assert PermissionModeName("dontask") is PermissionModeName.UNKNOWN


@pytest.mark.parametrize(
    "channel",
    [
        "workflow_event",
        "todos_updated",
        "plan_decided",
        "http_retry_attempt",
        "http_retry_succeeded",
        "visualization_action_dispatched",
        "host_signal",
        "window_model_changed",
        "open_external",
        "workspace_trust_required",
        "vm_credential",
        "notifications_changed",
        "scheduler_started",
        "orchestrate_event",
        "file_edited",
        "background_process_done",
    ],
)
def test_the_channels_the_front_end_routes_on_are_members(channel):
    assert PushChannel(channel).value == channel
    assert PushChannel(channel) is not PushChannel.UNKNOWN


def test_every_member_value_is_unique_and_a_plain_string():
    for enum in [*INGEST_ENUMS, CommandResultKind, CommandAction]:
        values = [m.value for m in enum]
        assert len(values) == len(set(values))
        assert all(isinstance(m, str) for m in enum)
