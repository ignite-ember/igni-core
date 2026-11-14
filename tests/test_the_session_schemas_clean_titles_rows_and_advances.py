"""The session package's own wire models: the title cleaner, the list row,
the loop-advance variants, the switch results, the banner and the small envelopes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from igni_core.core.session.schemas import (
    CompactResult,
    ContextBreakdown,
    ForkResult,
    InteractiveBanner,
    LoadResult,
    LoopAdvance,
    LoopPhase,
    McpClientBundle,
    McpInitResult,
    McpServerStatus,
    MessageMedia,
    OutputStyleSwitch,
    PermissionModeSwitch,
    PersistResult,
    PlanDecisionResult,
    PluginReloadCounts,
    PostCompactHookPayload,
    PreCompactHookPayload,
    SessionLifecyclePayload,
    SessionListRow,
    SessionTitle,
    StopFailureHookPayload,
    StopHookPayload,
    UserPromptSubmitHookPayload,
)
from igni_core.core.tools.plan import PlanDecision

# ── SessionTitle ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        ("**Fix the build**", "Fix the build"),
        ("# Fix the build", "Fix the build"),
        ('  "Fix the build"  ', "Fix the build"),
        ("`_Fix the build_`", "Fix the build"),
        ("Fix the build #", "Fix the build #"),
        ("Fix 'the' build", "Fix 'the' build"),
        ("Ремонт збірки", "Ремонт збірки"),
        ("***", ""),
        ("", ""),
    ],
)
def test_a_generated_title_loses_its_decoration_and_keeps_the_rest(raw, cleaned):
    assert SessionTitle.clean(raw) == cleaned


def test_the_raw_title_is_kept_beside_the_cleaned_one():
    title = SessionTitle(raw='"Title"')
    assert (title.raw, title.cleaned) == ('"Title"', "Title")


def test_a_title_that_is_not_text_becomes_empty():
    stray = SessionTitle.model_construct(raw=7, cleaned="")
    assert stray._populate_cleaned().cleaned == ""
    assert stray.raw == ""


# ── SessionListRow ───────────────────────────────────────────────


def _agno(**over):
    base = {
        "session_id": "s1",
        "runs": [object(), object()],
        "summary": SimpleNamespace(summary="did things"),
        "agent_data": {"name": "igni"},
        "session_data": {"session_name": "Build fix"},
        "created_at": 10,
        "updated_at": 20,
    }
    return SimpleNamespace(**(base | over))


def test_a_row_reads_every_field_of_an_agno_session():
    assert SessionListRow.from_agno(_agno()).model_dump() == {
        "session_id": "s1",
        "name": "Build fix",
        "created_at": 10,
        "updated_at": 20,
        "run_count": 2,
        "summary": "did things",
        "agent_name": "igni",
    }


@pytest.mark.parametrize("empty", [None, "text", [], 0])
def test_a_row_tolerates_rows_written_by_earlier_versions(empty):
    row = SessionListRow.from_agno(
        _agno(
            runs=None,
            summary=empty,
            agent_data=empty,
            session_data=empty,
            created_at=None,
            updated_at=None,
        )
    )
    assert (row.run_count, row.summary, row.agent_name, row.name) == (0, "", "", "")
    assert (row.created_at, row.updated_at) == (0, 0)


def test_a_summary_without_text_gives_an_empty_summary():
    assert SessionListRow.from_agno(_agno(summary=SimpleNamespace(summary=None))).summary == ""
    assert SessionListRow.from_agno(_agno(summary=SimpleNamespace())).summary == ""


# ── Envelopes ────────────────────────────────────────────────────


def test_the_envelopes_default_to_success_with_nothing_in_them():
    assert LoadResult().model_dump() == {"ok": True, "value": None, "error": None}
    assert PersistResult().model_dump() == {"ok": True, "error": None}
    assert ForkResult().model_dump() == {"ok": True, "new_session_id": "", "error": None}
    assert CompactResult().model_dump() == {
        "ok": True,
        "status": "",
        "summary": "",
        "error": None,
        "history_cleared": False,
    }


def test_a_compaction_can_fail_and_still_have_cleared_the_history():
    result = CompactResult(ok=False, error="summariser raised", history_cleared=True)
    assert (result.ok, result.history_cleared) == (False, True)


@pytest.mark.parametrize(("total", "runs", "floor"), [(100, 30, 70), (30, 100, 0), (0, 0, 0)])
def test_the_context_floor_is_what_history_does_not_explain_and_never_negative(total, runs, floor):
    assert ContextBreakdown.from_totals(total, runs).model_dump() == {
        "total": total,
        "runs": runs,
        "floor": floor,
    }


# ── The banner ───────────────────────────────────────────────────


class _Recorder:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def print_welcome(self, version):
        self.lines.append(("welcome", version))

    def print_info(self, message):
        self.lines.append(("info", message))

    def print_warning(self, message):
        self.lines.append(("warning", message))


def test_a_bare_banner_has_a_welcome_a_tip_and_the_session():
    out = _Recorder()
    InteractiveBanner(version="1.2", tip="use /help", session_id="abc").render(out)
    assert out.lines == [
        ("welcome", "1.2"),
        ("info", "Tip: use /help"),
        ("info", "Session: abc"),
    ]


def test_a_full_banner_lists_what_was_loaded_in_a_fixed_order():
    out = _Recorder()
    InteractiveBanner(
        version="1.2",
        tip="t",
        update_message="1.3 is out",
        agent_names=["a", "b"],
        skill_names=["review", "ship"],
        hook_count=3,
        session_id="abc",
        resumed=True,
    ).render(out)
    assert out.lines == [
        ("welcome", "1.2"),
        ("info", "Tip: t"),
        ("warning", "1.3 is out"),
        ("info", "Loaded agents: a, b"),
        ("info", "Loaded skills: /review, /ship"),
        ("info", "Loaded hooks: 3"),
        ("info", "Session: abc (resumed)"),
    ]


# ── /loop ────────────────────────────────────────────────────────


def test_the_loop_phases_have_their_wire_names():
    assert [p.value for p in LoopPhase] == ["idle", "running", "paused"]


def test_a_loop_advance_is_a_step_unless_it_says_otherwise():
    assert LoopAdvance().kind == "step"


def test_a_finished_loop_carries_only_its_count():
    advance = LoopAdvance.completed_at(5)
    assert (
        advance.model_dump()
        == LoopAdvance(kind="completed", completed=True, total_iterations=5).model_dump()
    )
    assert (advance.kind, advance.completed, advance.total_iterations) == ("completed", True, 5)
    assert (advance.safety_cap_paused, advance.prompt, advance.iteration) == (False, "", 0)


def test_a_loop_that_hit_the_safety_limit_carries_the_iteration_it_reached():
    advance = LoopAdvance.safety_paused(50)
    assert (advance.kind, advance.safety_cap_paused, advance.iteration) == (
        "safety_paused",
        True,
        50,
    )
    assert (advance.completed, advance.total_iterations, advance.prompt) == (False, 0, "")


def test_a_step_carries_its_prompts_and_counters():
    advance = LoopAdvance.step(
        prompt="wrapped",
        display_prompt="bare",
        iteration=2,
        remaining=3,
        cap_explicit=True,
        auto_extended=True,
    )
    assert advance.model_dump() == {
        "kind": "step",
        "completed": False,
        "total_iterations": 0,
        "safety_cap_paused": False,
        "prompt": "wrapped",
        "display_prompt": "bare",
        "iteration": 2,
        "remaining": 3,
        "cap_explicit": True,
        "auto_extended": True,
    }


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "completed", "safety_cap_paused": True},
        {"kind": "completed", "prompt": "p"},
        {"kind": "completed", "display_prompt": "p"},
        {"kind": "safety_paused", "completed": True},
        {"kind": "safety_paused", "total_iterations": 1},
        {"kind": "safety_paused", "prompt": "p"},
        {"kind": "safety_paused", "display_prompt": "p"},
        {"kind": "step", "completed": True},
        {"kind": "step", "total_iterations": 1},
        {"kind": "step", "safety_cap_paused": True},
        {"safety_cap_paused": True},
    ],
)
def test_an_advance_cannot_mix_the_fields_of_two_outcomes(fields):
    with pytest.raises(ValidationError, match="cannot carry"):
        LoopAdvance(**fields)


def test_an_unknown_advance_kind_is_refused():
    with pytest.raises(ValidationError):
        LoopAdvance(kind="stopped")


# ── Switch results ───────────────────────────────────────────────


@pytest.mark.parametrize("model", [PermissionModeSwitch, OutputStyleSwitch])
def test_a_switch_result_defaults_to_neither_refused_nor_changed_and_is_frozen(model):
    result = model(text="done")
    assert (result.refused, result.changed, result.text) == (False, False, "done")
    with pytest.raises(ValidationError):
        result.changed = True


@pytest.mark.parametrize("model", [PermissionModeSwitch, OutputStyleSwitch])
def test_a_switch_result_must_say_something(model):
    with pytest.raises(ValidationError):
        model()


# ── MCP + media ──────────────────────────────────────────────────


def test_a_client_bundle_lists_its_names_sorted_and_counts_its_clients():
    bundle = McpClientBundle(clients={"zeta": object(), "alpha": object()})
    assert bundle.names == ["alpha", "zeta"]
    assert len(bundle) == 2
    assert bundle


def test_an_empty_client_bundle_is_falsy():
    bundle = McpClientBundle()
    assert (bundle.names, len(bundle), bool(bundle)) == ([], 0, False)


def test_an_mcp_init_result_defaults_to_nothing_connected():
    assert McpInitResult().model_dump() == {
        "connected": [],
        "failed": {},
        "rebuilt": False,
        "skipped_reason": None,
    }


@pytest.mark.parametrize(
    "reason", ["already_initialized", "no_configured_servers", "no_clients_connected"]
)
def test_the_known_skip_reasons_are_accepted(reason):
    assert McpInitResult(skipped_reason=reason).skipped_reason == reason


def test_an_unknown_skip_reason_is_refused():
    with pytest.raises(ValidationError):
        McpInitResult(skipped_reason="gave_up")


def test_media_with_nothing_attached_adds_no_keyword_to_the_run():
    assert MessageMedia().to_kwargs() == {}


@pytest.mark.parametrize("field", ["images", "audio", "videos", "files"])
def test_an_empty_media_list_is_still_passed_on(field):
    assert MessageMedia(**{field: []}).to_kwargs() == {field: []}


def test_every_kind_of_media_is_passed_under_its_own_name():
    media = MessageMedia(images=["i"], audio=["a"], videos=["v"], files=["f"])
    assert media.to_kwargs() == {"images": ["i"], "audio": ["a"], "videos": ["v"], "files": ["f"]}


# ── Hook payloads and small wire rows ────────────────────────────


def test_hook_payloads_carry_their_own_typed_fields():
    assert UserPromptSubmitHookPayload(message="hi", session_id="s").model_dump() == {
        "message": "hi",
        "session_id": "s",
    }
    assert StopHookPayload(session_id="s", response="done").model_dump() == {
        "session_id": "s",
        "response": "done",
    }
    assert StopFailureHookPayload(
        session_id="s", error="boom", error_type="RuntimeError"
    ).model_dump() == {"session_id": "s", "error": "boom", "error_type": "RuntimeError"}
    assert PreCompactHookPayload(session_id="s", scope="auto", tokens_before=9).model_dump() == {
        "session_id": "s",
        "scope": "auto",
        "tokens_before": 9,
    }


def test_a_hook_payload_missing_a_field_is_refused():
    with pytest.raises(ValidationError):
        StopHookPayload(session_id="s")
    with pytest.raises(ValidationError):
        PreCompactHookPayload(session_id="s", scope="auto", tokens_before="many")


def test_a_post_compact_payload_keeps_what_a_subscriber_adds():
    payload = PostCompactHookPayload(session_id="s", scope="manual", tokens_before=5, extra="x")
    assert payload.summary_chars == 0
    assert payload.model_dump()["extra"] == "x"


def test_a_lifecycle_payload_is_fresh_unless_resumed():
    assert SessionLifecyclePayload(session_id="s").model_dump() == {
        "session_id": "s",
        "resumed": False,
    }


def test_a_plugin_reload_names_all_four_counts():
    with pytest.raises(ValidationError):
        PluginReloadCounts(plugins=1, skills=2, agents=3)
    assert PluginReloadCounts(plugins=1, skills=2, agents=3, hooks=4).model_dump() == {
        "plugins": 1,
        "skills": 2,
        "agents": 3,
        "hooks": 4,
    }


def test_a_plan_decision_succeeds_with_no_mode_change_unless_told_otherwise():
    result = PlanDecisionResult(run_id="r", decision=PlanDecision.APPROVED)
    assert result.model_dump(mode="json") == {
        "run_id": "r",
        "decision": "approved",
        "mode_status": "",
        "ok": True,
        "error": None,
    }


def test_an_mcp_server_status_names_the_server_and_whether_it_is_connected():
    assert McpServerStatus(name="notes", connected=True).model_dump() == {
        "name": "notes",
        "connected": True,
    }
