"""A text-only model that tries to read a media file anyway is refused, with the metadata.

Told vision is disabled, a model may still run ``cat shot.png`` or ``base64``
it. The guard knows the media files the current turn named, matches them by
file name and media extension, and refuses a read tool's call on one, or a
shell line in which a dumping or OCR command is given one. Its refusal is
the tool result: vision is disabled, here is the metadata. A vision model's
turn names nothing to the guard, so nothing is refused.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.core.config.permission_eval import PermissionEvaluator
from igni_core.core.hooks.executor import HookExecutor
from igni_core.core.hooks.hook_firer import HookFirer
from igni_core.core.hooks.permission.context import ToolCallContext
from igni_core.core.hooks.permission.outcome import Block, Continue
from igni_core.core.hooks.permission.pipeline import PermissionPipeline
from igni_core.core.hooks.permission.vision_disabled_stage import VisionDisabledStage
from igni_core.core.hooks.text_only_media_guard import TextOnlyMediaGuard
from igni_core.core.hooks.tool_hook import ToolEventHook
from igni_core.core.utils.media_metadata import MediaMetadataReader
from igni_core.core.utils.media_schemas import MediaFileMetadata
from igni_core.core.utils.tool_audit import DecidedBy
from tests.session_factory import build_session

PNG = (Path(__file__).parent / "fixtures" / "media" / "seven-by-three.png").read_bytes()
REFUSAL = "Not run: vision is disabled for this model; only the metadata below is available."
UNATTACHED_REFUSAL = (
    "Not run: this media file could not be attached; only the metadata below is available."
)


@pytest.fixture
def shot(tmp_path: Path) -> MediaFileMetadata:
    (tmp_path / "shot.png").write_bytes(PNG)
    metadata = MediaMetadataReader(tmp_path).read("shot.png")
    assert metadata is not None
    return metadata


@pytest.fixture
def clip(tmp_path: Path) -> MediaFileMetadata:
    (tmp_path / "clip.mp4").write_bytes(b"\x00" * 4)
    metadata = MediaMetadataReader(tmp_path).read("clip.mp4")
    assert metadata is not None
    return metadata


@pytest.fixture
def guard(shot: MediaFileMetadata, clip: MediaFileMetadata) -> TextOnlyMediaGuard:
    guard = TextOnlyMediaGuard()
    guard.begin_turn([shot, clip])
    return guard


def _shell(command: str) -> dict[str, Any]:
    return {"command": command}


def test_the_refusal_is_worded_exactly_as_approved():
    assert TextOnlyMediaGuard.REFUSAL == REFUSAL


def test_the_refusal_for_a_file_a_vision_model_was_not_sent_is_worded_exactly_as_approved():
    assert TextOnlyMediaGuard.UNATTACHED_REFUSAL == UNATTACHED_REFUSAL


@pytest.mark.parametrize(
    ("tool", "args"),
    [("run_shell_command", _shell("cat shot.png")), ("read_file", {"file_path": "shot.png"})],
)
def test_a_vision_models_unattached_file_is_refused_as_not_attached(shot, clip, tool, args):
    guard = TextOnlyMediaGuard()
    guard.begin_turn([], [shot, clip])

    refusal = guard.refusal(tool, args)

    assert refusal == f"{UNATTACHED_REFUSAL}\n{shot.describe()}"
    assert guard.files == [shot, clip]


def test_each_file_is_refused_for_why_the_model_cannot_see_it(shot, clip):
    guard = TextOnlyMediaGuard()
    guard.begin_turn([shot], [clip])

    refusal = guard.refusal("run_shell_command", _shell("cat clip.mp4 shot.png"))

    assert refusal == (f"{REFUSAL}\n{shot.describe()}\n{UNATTACHED_REFUSAL}\n{clip.describe()}")


def test_the_next_turn_drops_the_unattached_files(shot, clip):
    guard = TextOnlyMediaGuard()
    guard.begin_turn([shot], [clip])

    guard.begin_turn([])

    assert guard.files == []
    assert guard.refusal("run_shell_command", _shell("cat clip.mp4")) is None


def test_cat_on_the_image_is_refused_with_its_metadata(guard, shot):
    refusal = guard.refusal("run_shell_command", _shell("cat shot.png"))

    assert refusal == f"{REFUSAL}\n{shot.describe()}"
    assert refusal.endswith("type: image/png, size: 74 bytes, dimensions: 7x3 px)")


@pytest.mark.parametrize(
    "command",
    [
        "cat shot.png",
        "xxd shot.png | head",
        "base64 < shot.png",
        "base64 --input=shot.png",
        "strings shot.png",
        "tesseract shot.png out",
        "head -c 200 {abs}",
        "tail {abs}",
        "less shot.png",
        "more shot.png",
        "bat shot.png",
        "hexdump -C shot.png",
        "od -c shot.png",
        "cd /tmp && cat shot.png",
        "ls; cat 'shot.png'",
        "LANG=C cat shot.png",
        "sudo cat shot.png",
        "env cat shot.png",
        "time cat shot.png",
        "nice cat shot.png",
        "command cat shot.png",
        "/bin/cat shot.png",
        "(cat shot.png)",
        "true || cat shot.png",
        "sleep 1 & cat shot.png",
        "cat<shot.png",
        "echo hi | cat shot.png",
        "ls |& cat shot.png",
        "true && cat shot.png",
        "cat ./sub/../shot.png",
    ],
)
def test_each_way_of_dumping_or_ocring_the_image_is_refused(guard, shot, command: str):
    refusal = guard.refusal("run_shell_command", _shell(command.format(abs=shot.path)))

    assert refusal == f"{REFUSAL}\n{shot.describe()}"


def test_a_line_that_dumps_both_files_names_both(guard, shot, clip):
    refusal = guard.refusal("run_shell_command", _shell("cat clip.mp4 shot.png"))

    assert refusal == f"{REFUSAL}\n{shot.describe()}\n{clip.describe()}"


def test_the_command_may_arrive_as_a_list(guard, shot):
    refusal = guard.refusal("run_shell_command", {"command": ["cat", "shot.png"]})

    assert refusal == f"{REFUSAL}\n{shot.describe()}"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("cp shot.png assets/logo.png", id="copying-is-not-reading"),
        pytest.param("mv shot.png done/", id="moving-is-not-reading"),
        pytest.param("ls -la shot.png", id="listing-is-not-reading"),
        pytest.param("ls shot.png | cat", id="the-reader-is-not-given-the-file"),
        pytest.param("cat notes.md", id="a-file-that-is-not-media"),
        pytest.param("cat other.png", id="a-media-file-the-turn-did-not-name"),
        pytest.param("cat shot.png.txt", id="not-a-media-extension"),
        pytest.param("cat 'shot.png", id="an-unparseable-line"),
        pytest.param("FOO=1", id="an-assignment-alone"),
        pytest.param("sudo", id="a-prefix-alone"),
        pytest.param("", id="empty"),
    ],
)
def test_a_line_that_does_not_dump_a_named_media_file_runs(guard, command: str):
    assert guard.refusal("run_shell_command", _shell(command)) is None


@pytest.mark.parametrize("key", ["file_path", "file_name", "path"])
@pytest.mark.parametrize("tool", ["read_file", "read_file_chunk"])
def test_a_read_tool_given_the_image_is_refused(guard, shot, tool: str, key: str):
    assert guard.refusal(tool, {key: shot.path}) == f"{REFUSAL}\n{shot.describe()}"


def test_the_first_path_argument_is_the_one_read(guard):
    args = {"file_path": "notes.md", "path": "shot.png"}

    assert guard.refusal("read_file", args) is None


@pytest.mark.parametrize(
    "args", [{"file_path": "notes.md"}, {"file_path": "other.png"}, {"file_path": ""}, {}]
)
def test_a_read_tool_given_anything_else_runs(guard, args):
    assert guard.refusal("read_file", args) is None


def test_a_read_tool_given_a_non_string_path_runs(guard):
    assert guard.refusal("read_file", {"file_path": 3}) is None


@pytest.mark.parametrize("tool", ["save_file", "notebook_read", "read_process_output", "grep"])
def test_another_tool_naming_the_image_runs(guard, tool: str):
    assert (
        guard.refusal(
            tool, {"file_name": "shot.png", "file_path": "shot.png", "command": "cat shot.png"}
        )
        is None
    )


def test_a_vision_models_turn_guards_nothing(guard):
    guard.begin_turn([])

    assert guard.files == []
    assert guard.refusal("run_shell_command", _shell("cat shot.png")) is None
    assert guard.refusal("read_file", {"file_path": "shot.png"}) is None


def test_the_next_turn_replaces_what_is_guarded(guard, clip):
    guard.begin_turn([clip])

    assert guard.refusal("run_shell_command", _shell("cat shot.png")) is None
    assert guard.refusal("run_shell_command", _shell("cat clip.mp4")) is not None


def test_the_guarded_files_are_a_copy(guard, shot, clip):
    guard.files.clear()

    assert guard.files == [shot, clip]


def test_a_new_guard_guards_nothing():
    assert TextOnlyMediaGuard().refusal("run_shell_command", _shell("cat shot.png")) is None


# ── the stage and the pipeline ────────────────────────────────────


async def test_the_stage_blocks_as_vision_disabled(guard, shot):
    outcome = await VisionDisabledStage(guard).evaluate(
        ToolCallContext.for_check("run_shell_command", _shell("cat shot.png"))
    )

    assert outcome == Block(f"{REFUSAL}\n{shot.describe()}")
    assert isinstance(outcome, Block) and outcome.by is DecidedBy.VISION_DISABLED


async def test_the_stage_lets_anything_else_through(guard):
    outcome = await VisionDisabledStage(guard).evaluate(
        ToolCallContext.for_check("run_shell_command", _shell("ls"))
    )

    assert outcome == Continue()


def test_a_hooks_allow_does_not_skip_the_stage():
    assert VisionDisabledStage.skipped_by_hook_allow is False


def _pipeline(guard: TextOnlyMediaGuard | None) -> PermissionPipeline:
    return PermissionPipeline.build(
        firer=HookFirer(HookExecutor({}), "s"),
        protected_paths=[],
        blocked_commands=["cat shot"],
        permission_evaluator=PermissionEvaluator.from_strings(deny=["Bash(cat *)"]),
        media_guard=guard,
    )


async def test_the_pipeline_refuses_after_the_safety_lists_and_before_the_evaluator(guard):
    """The blocked-command list still wins; the evaluator's deny never gets the call."""
    blocked = await _pipeline(guard).decide(
        ToolCallContext.for_check("run_shell_command", _shell("cat shot.png"))
    )
    refused = await _pipeline(guard).decide(
        ToolCallContext.for_check("run_shell_command", _shell("xxd shot.png"))
    )
    denied = await _pipeline(guard).decide(
        ToolCallContext.for_check("run_shell_command", _shell("cat notes.md"))
    )

    assert blocked.by is DecidedBy.BLOCKED_COMMAND
    assert refused.by is DecidedBy.VISION_DISABLED
    assert refused.message is not None and refused.message.startswith(REFUSAL)
    assert denied.by is DecidedBy.RULE


async def test_a_pipeline_without_a_guard_has_no_such_stage():
    verdict = await _pipeline(None).decide(
        ToolCallContext.for_check("run_shell_command", _shell("xxd shot.png"))
    )

    assert verdict.by is not DecidedBy.VISION_DISABLED


async def test_the_evaluator_never_asks_about_a_refused_call(guard):
    ask = AsyncMock()
    pipeline = PermissionPipeline.build(
        firer=HookFirer(HookExecutor({}), "s"),
        protected_paths=[],
        blocked_commands=[],
        permission_evaluator=PermissionEvaluator.from_strings(ask=["Bash(*)"]),
        ask_person=ask,
        media_guard=guard,
    )

    verdict = await pipeline.decide(
        ToolCallContext.for_check("run_shell_command", _shell("cat shot.png"))
    )

    assert verdict.by is DecidedBy.VISION_DISABLED
    ask.assert_not_awaited()


# ── the tool hook ─────────────────────────────────────────────────


class _Ran:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, **_: Any) -> str:
        self.calls += 1
        return "bytes"


async def test_the_tool_hook_returns_the_refusal_and_never_runs_the_tool(guard, shot):
    audit = MagicMock()
    hook = ToolEventHook(HookExecutor({}), session_id="s", audit=audit, media_guard=guard)
    tool = _Ran()

    result = await hook(name="run_shell_command", func=tool, args=_shell("cat shot.png"))

    assert result == f"{REFUSAL}\n{shot.describe()}"
    assert tool.calls == 0
    assert audit.refused.call_args.kwargs["decided_by"] is DecidedBy.VISION_DISABLED


async def test_the_tool_hook_runs_the_same_call_for_a_vision_model(guard):
    guard.begin_turn([])
    hook = ToolEventHook(HookExecutor({}), session_id="s", media_guard=guard)
    tool = _Ran()

    result = await hook(name="run_shell_command", func=tool, args=_shell("cat shot.png"))

    assert "bytes" in result and not result.startswith("Not run:")
    assert tool.calls == 1


# ── a real session ────────────────────────────────────────────────


async def test_a_sessions_tool_hook_uses_the_sessions_guard(tmp_path: Path):
    session = build_session(tmp_path)
    (session.project_dir / "shot.png").write_bytes(PNG)
    metadata = MediaMetadataReader(session.project_dir).read("shot.png")
    assert metadata is not None

    assert session.tool_hook_factory.media_guard is session.media_guard
    session.media_guard.begin_turn([metadata])
    tool = _Ran()
    result = await session.tool_hook_factory.create()(
        name="run_shell_command", func=tool, args=_shell("cat shot.png")
    )

    assert result == f"{REFUSAL}\n{metadata.describe()}"
    assert tool.calls == 0


async def test_a_sessions_tool_hook_refuses_an_unattached_file_as_not_attached(tmp_path: Path):
    session = build_session(tmp_path)
    (session.project_dir / "shot.png").write_bytes(PNG)
    metadata = MediaMetadataReader(session.project_dir).read("shot.png")
    assert metadata is not None

    session.media_guard.begin_turn([], [metadata])
    tool = _Ran()
    result = await session.tool_hook_factory.create()(
        name="run_shell_command", func=tool, args=_shell("cat shot.png")
    )

    assert result == f"{UNATTACHED_REFUSAL}\n{metadata.describe()}"
    assert tool.calls == 0


@pytest.mark.parametrize("skipped", [3, "", None, ["shot.png"]])
def test_a_path_argument_that_is_not_a_path_is_passed_over(guard, shot, skipped):
    refusal = guard.refusal("read_file", {"file_path": skipped, "path": "shot.png"})

    assert refusal == f"{REFUSAL}\n{shot.describe()}"


def test_a_file_name_with_shell_word_breaks_is_one_operand(tmp_path: Path):
    (tmp_path / "logo@2x+dark.png").write_bytes(PNG)
    metadata = MediaMetadataReader(tmp_path).read("logo@2x+dark.png")
    assert metadata is not None
    guard = TextOnlyMediaGuard()
    guard.begin_turn([metadata])

    refusal = guard.refusal("run_shell_command", _shell("cat logo@2x+dark.png"))

    assert refusal == f"{REFUSAL}\n{metadata.describe()}"
