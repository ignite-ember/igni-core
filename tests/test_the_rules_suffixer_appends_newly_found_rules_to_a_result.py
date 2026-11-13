"""RulesSuffixer: the discovered-rules block, the paths it reads and the event it fires.

A real :class:`RulesIndex` over real files; the firer's executor only
records what it is asked to fire.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.executor import HookExecutor
from igni_core.core.hooks.hook_firer import HookFirer
from igni_core.core.hooks.permission.context import ToolCallContext
from igni_core.core.hooks.rules_suffixer import RulesSuffixer
from igni_core.core.hooks.schemas import HookDefinition, HookPayload, HookResult
from igni_core.core.paths import RULES_FILES
from igni_core.core.utils.rules_index import RulesIndex

RULES_NAME = RULES_FILES[0]


class _Recorder(HookExecutor):
    def __init__(self) -> None:
        super().__init__({})
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def get_matching_hooks(self, event: str, target: str = "") -> list[HookDefinition]:
        return [HookDefinition(type="command", command=":")]

    async def execute(self, event: str, payload: BaseModel, target: str = "") -> HookResult:
        self.calls.append((event, target, HookPayload(body=payload).to_wire_dict()))
        return HookResult(should_continue=True)


def _ctx(**args: Any) -> ToolCallContext:
    return ToolCallContext(name="read_file", func=None, args=args)


def _rules(folder: Path, text: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / RULES_NAME
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    folder = (tmp_path / "project").resolve()
    folder.mkdir()
    return folder


def _suffixer(project: Path | None, index: RulesIndex | None) -> tuple[RulesSuffixer, _Recorder]:
    recorder = _Recorder()
    return RulesSuffixer(index, project, HookFirer(recorder, "sess-1")), recorder


async def test_a_new_rules_file_is_appended_with_a_relative_label_and_fires_the_event(project):
    _rules(project / "svc", "  be careful here  \n\n")
    index = RulesIndex(project)
    suffixer, recorder = _suffixer(project, index)

    result = await suffixer.enrich(_ctx(file_path=str(project / "svc" / "main.py")), "read ok")

    assert result == (
        f'read ok\n\n<discovered-rules path="svc/{RULES_NAME}">\nbe careful here\n</discovered-rules>'
    )
    ((event, target, payload),) = recorder.calls
    assert (event, target) == (HookEvent.INSTRUCTIONS_LOADED.value, "")
    assert payload["source"] == "rules_index"
    assert payload["session_id"] == "sess-1"
    assert payload["files"] == [f"svc/{RULES_NAME}"]
    assert payload["bytes"] == len("  be careful here  \n\n")


async def test_each_rules_file_surfaces_once_and_the_repeat_is_untouched_and_silent(project):
    _rules(project / "svc", "rules")
    suffixer, recorder = _suffixer(project, RulesIndex(project))
    ctx = _ctx(file_path=str(project / "svc" / "a.py"))

    first = await suffixer.enrich(ctx, "one")
    second = await suffixer.enrich(ctx, "two")

    assert "<discovered-rules" in first
    assert second == "two"
    assert len(recorder.calls) == 1


async def test_several_rules_files_join_with_a_newline_shallowest_first_and_bytes_count_utf8(
    project,
):
    _rules(project / "a", "é")
    _rules(project / "a" / "b", "ü ü")
    suffixer, recorder = _suffixer(project, RulesIndex(project))

    result = await suffixer.enrich(_ctx(path=str(project / "a" / "b" / "x.py")), "r")

    assert result == (
        f'r\n\n<discovered-rules path="a/{RULES_NAME}">\né\n</discovered-rules>\n'
        f'<discovered-rules path="a/b/{RULES_NAME}">\nü ü\n</discovered-rules>'
    )
    ((_, _, payload),) = recorder.calls
    assert payload["files"] == [f"a/{RULES_NAME}", f"a/b/{RULES_NAME}"]
    assert payload["bytes"] == len("é".encode()) + len("ü ü".encode()) == 2 + 5


@pytest.mark.parametrize("key", ["file_path", "path", "filename", "directory", "dir"])
async def test_each_of_the_five_path_arguments_is_consulted(project, key):
    _rules(project / "svc", "rules")
    suffixer, _ = _suffixer(project, RulesIndex(project))

    result = await suffixer.enrich(_ctx(**{key: str(project / "svc" / "f.txt")}), "r")

    assert "<discovered-rules" in result


async def test_a_relative_path_is_resolved_against_the_project_dir(project):
    _rules(project / "svc", "rules")
    suffixer, _ = _suffixer(project, RulesIndex(project))

    result = await suffixer.enrich(_ctx(file_path="svc/main.py"), "r")

    assert f'path="svc/{RULES_NAME}"' in result


async def test_the_same_file_named_by_two_arguments_surfaces_once(project):
    _rules(project / "svc", "rules")
    suffixer, recorder = _suffixer(project, RulesIndex(project))
    target = str(project / "svc" / "f.txt")

    result = await suffixer.enrich(_ctx(file_path=target, path=target), "r")

    assert result.count("<discovered-rules") == 1
    assert len(recorder.calls) == 1


async def test_a_directory_argument_surfaces_that_directorys_rules(project):
    _rules(project / "svc", "rules")
    suffixer, _ = _suffixer(project, RulesIndex(project))

    result = await suffixer.enrich(_ctx(directory=str(project / "svc")), "r")

    assert f'path="svc/{RULES_NAME}"' in result


async def test_a_rules_file_outside_the_project_dir_keeps_its_absolute_label(tmp_path, project):
    outside = (tmp_path / "elsewhere").resolve()
    _rules(outside / "svc", "outside rules")
    index = RulesIndex(outside)
    suffixer, recorder = _suffixer(project, index)

    result = await suffixer.enrich(_ctx(file_path=str(outside / "svc" / "x.py")), "r")

    label = outside / "svc" / RULES_NAME
    assert f'<discovered-rules path="{label}">' in result
    assert recorder.calls[0][2]["files"] == [str(label)]


async def test_without_a_project_dir_a_relative_path_stays_relative_and_labels_are_absolute(
    project, monkeypatch
):
    _rules(project / "svc", "rules")
    index = RulesIndex(project)
    suffixer, _ = _suffixer(None, index)

    untouched = await suffixer.enrich(_ctx(file_path="svc/main.py"), "r")
    result = await suffixer.enrich(_ctx(file_path=str(project / "svc" / "main.py")), "r")

    assert untouched == "r"
    assert f'<discovered-rules path="{project / "svc" / RULES_NAME}">' in result


@pytest.mark.parametrize("result", [b"bytes", {"a": 1}, None, 5, ["x"]])
async def test_a_result_that_is_not_a_string_passes_through_and_consumes_nothing(project, result):
    _rules(project / "svc", "rules")
    suffixer, recorder = _suffixer(project, RulesIndex(project))
    ctx = _ctx(file_path=str(project / "svc" / "m.py"))

    assert await suffixer.enrich(ctx, result) is result
    assert recorder.calls == []
    # The rules were not consumed: the next string result still gets them.
    assert "<discovered-rules" in await suffixer.enrich(ctx, "later")


async def test_no_rules_index_passes_the_result_through(project):
    suffixer, recorder = _suffixer(project, None)

    result = await suffixer.enrich(_ctx(file_path=str(project / "m.py")), "r")

    assert result == "r"
    assert recorder.calls == []


@pytest.mark.parametrize(
    "args",
    [{}, {"command": "ls"}, {"file_path": ""}, {"file_path": 5, "path": None, "dir": ["x"]}],
)
async def test_no_usable_path_argument_passes_the_result_through(project, args):
    _rules(project / "svc", "rules")
    suffixer, recorder = _suffixer(project, RulesIndex(project))

    assert await suffixer.enrich(_ctx(**args), "r") == "r"
    assert recorder.calls == []


async def test_nothing_discovered_passes_the_result_through_and_fires_nothing(project):
    (project / "plain").mkdir()
    suffixer, recorder = _suffixer(project, RulesIndex(project))

    assert await suffixer.enrich(_ctx(file_path=str(project / "plain" / "m.py")), "r") == "r"
    assert recorder.calls == []
