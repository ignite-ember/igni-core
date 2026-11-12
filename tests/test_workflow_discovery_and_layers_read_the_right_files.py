"""Which workflow files a session reads, which copy of a name wins, and what is cached.

Real directories on disk, a real ``WorkflowLayers`` and ``WorkflowDiscovery``.
The two doubles are the ones discovery is handed: the runtime that probes a
file's ``meta`` (the node subprocess) and the folder-trust answer.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

import igni_core.backend.workflow_layers as layers_mod
from igni_core.backend.schemas_workflows import MetaProbeResult, WorkflowMeta
from igni_core.backend.workflow_discovery import WorkflowDiscovery
from igni_core.backend.workflow_layers import FallbackLayerNotice, WorkflowLayers
from igni_core.core.paths import CONFIG_DIR


class _Runtime:
    """Probes a file's meta: its first line names the workflow; ``FAIL`` fails."""

    def __init__(self) -> None:
        self.probed: list[Path] = []

    async def probe_meta(self, path: Path) -> MetaProbeResult:
        self.probed.append(path)
        text = path.read_text().strip()
        if text == "FAIL":
            return MetaProbeResult(reason="exit 1: boom")
        return MetaProbeResult(meta=WorkflowMeta(name=text or path.stem))


class _Trust:
    def __init__(self, trusted: bool) -> None:
        self.trusted = trusted
        self.asked: list[tuple[Path, str]] = []

    def is_trusted(self, project_dir: Path, *, what: str) -> bool:
        self.asked.append((Path(project_dir), what))
        return self.trusted


def _write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _discovery(
    project: Path,
    *,
    home: Path | None = None,
    group: Path | None = None,
    trusted: bool = True,
    runtime: _Runtime | None = None,
    notice: FallbackLayerNotice | None = None,
) -> tuple[WorkflowDiscovery, _Runtime]:
    runtime = runtime or _Runtime()
    disc = WorkflowDiscovery.for_project(
        project,
        group_dir=group,
        home_dir=home or project.parent / "nohome",
        trust=_Trust(trusted),  # type: ignore[arg-type]
        runtime=runtime,  # type: ignore[arg-type]
        notice=notice,
    )
    return disc, runtime


# ── the layers ────────────────────────────────────────────────────


def test_the_layers_are_user_then_group_then_claude_then_igni(tmp_path: Path) -> None:
    project, home, group = tmp_path / "p", tmp_path / "h", tmp_path / "g"
    files = [
        _write(home / CONFIG_DIR / "workflows" / "a.mjs"),
        _write(group / "a.mjs"),
        _write(project / ".claude" / "workflows" / "a.mjs"),
        _write(project / CONFIG_DIR / "workflows" / "a.mjs"),
    ]
    layers = WorkflowLayers.for_project(project, group_dir=group, home_dir=home)

    paths = layers.paths(trusted=True, notice=FallbackLayerNotice())

    assert paths == files
    assert WorkflowLayers.shadow(paths) == {"a": files[-1]}


def test_within_a_layer_files_are_sorted_and_only_mjs_files_count(tmp_path: Path) -> None:
    project = tmp_path / "p"
    wf = project / CONFIG_DIR / "workflows"
    for name in ("b.mjs", "a.mjs", "notes.md", "c.js"):
        _write(wf / name)
    layers = WorkflowLayers.for_project(project, home_dir=tmp_path / "h")

    names = [p.name for p in layers.paths(trusted=True, notice=FallbackLayerNotice())]

    assert names[:2] == ["a.mjs", "b.mjs"]
    assert "notes.md" not in names
    assert "c.js" not in names


def test_an_untrusted_folder_reads_only_the_user_and_group_layers(tmp_path: Path) -> None:
    project, home, group = tmp_path / "p", tmp_path / "h", tmp_path / "g"
    mine = _write(home / CONFIG_DIR / "workflows" / "mine.mjs")
    shared = _write(group / "shared.mjs")
    _write(project / ".claude" / "workflows" / "x.mjs")
    _write(project / CONFIG_DIR / "workflows" / "y.mjs")
    layers = WorkflowLayers.for_project(project, group_dir=group, home_dir=home)
    notice = FallbackLayerNotice()

    assert layers.paths(trusted=False, notice=notice) == [mine, shared]
    assert notice.noted is False


def test_missing_directories_are_skipped_and_the_home_default_is_the_users_home(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "realhome"))
    layers = WorkflowLayers.for_project(tmp_path / "p")

    assert layers.user == Path.home() / CONFIG_DIR / "workflows"
    assert layers.group is None
    assert layers.paths(trusted=True, notice=FallbackLayerNotice()) == []


def test_an_empty_claude_directory_is_not_a_reason_to_say_it_is_deprecated(
    tmp_path: Path, caplog
) -> None:
    project = tmp_path / "p"
    (project / ".claude" / "workflows").mkdir(parents=True)
    _write(project / ".claude" / "workflows" / "readme.md", "not a workflow")
    layers = WorkflowLayers.for_project(project, home_dir=tmp_path / "h")
    notice = FallbackLayerNotice()

    with caplog.at_level(logging.WARNING, logger=layers_mod.__name__):
        assert layers.paths(trusted=True, notice=notice) == []

    assert notice.noted is False
    assert caplog.records == []


def test_the_deprecation_note_names_both_directories_and_is_said_once_per_notice(
    tmp_path: Path, caplog
) -> None:
    project = tmp_path / "p"
    _write(project / ".claude" / "workflows" / "old.mjs")
    layers = WorkflowLayers.for_project(project, home_dir=tmp_path / "h")
    notice = FallbackLayerNotice()

    with caplog.at_level(logging.WARNING, logger=layers_mod.__name__):
        layers.paths(trusted=True, notice=notice)
        layers.paths(trusted=True, notice=notice)
        layers.paths(trusted=True, notice=FallbackLayerNotice())

    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 2  # one per notice instance, never two from one
    assert str(project / ".claude" / "workflows") in messages[0]
    assert str(project / CONFIG_DIR / "workflows") in messages[0]


def test_the_project_path_is_shown_relative_and_a_path_outside_it_absolute(
    tmp_path: Path,
) -> None:
    project = tmp_path / "p"
    layers = WorkflowLayers.for_project(project, home_dir=tmp_path / "h")
    inside = project / CONFIG_DIR / "workflows" / "a.mjs"
    outside = tmp_path / "h" / CONFIG_DIR / "workflows" / "b.mjs"

    assert layers.display_path(inside) == f"{CONFIG_DIR}/workflows/a.mjs"
    assert layers.display_path(outside) == str(outside)


def test_not_found_names_the_project_and_the_user_directories_and_says_when_untrusted(
    tmp_path: Path,
) -> None:
    layers = WorkflowLayers.for_project(tmp_path / "p", home_dir=tmp_path / "h")
    project_dir = tmp_path / "p" / CONFIG_DIR / "workflows"
    user_dir = tmp_path / "h" / CONFIG_DIR / "workflows"

    assert layers.not_found_message("x", trusted=True) == (
        f"workflow 'x' not found in {project_dir} or {user_dir}"
    )
    assert layers.not_found_message("x", trusted=False) == (
        f"workflow 'x' not found in {project_dir} "
        f"(this folder is not trusted, so its workflows are not read) or {user_dir}"
    )


def test_the_layers_are_frozen(tmp_path: Path) -> None:
    layers = WorkflowLayers.for_project(tmp_path / "p")

    with pytest.raises(ValidationError):
        layers.user = tmp_path  # type: ignore[misc]


# ── discovery ─────────────────────────────────────────────────────


async def test_listing_is_one_per_name_sorted_and_skips_a_workflow_that_cannot_be_read(
    tmp_path: Path,
) -> None:
    project, home = tmp_path / "p", tmp_path / "h"
    _write(home / CONFIG_DIR / "workflows" / "zeta.mjs", "zeta-user")
    _write(home / CONFIG_DIR / "workflows" / "alpha.mjs", "alpha-user")
    _write(project / CONFIG_DIR / "workflows" / "alpha.mjs", "alpha-project")
    _write(project / CONFIG_DIR / "workflows" / "broken.mjs", "FAIL")
    disc, _ = _discovery(project, home=home)

    listed = await disc.list_workflows()

    assert [e.meta.name for e in listed] == ["alpha-project", "zeta-user"]


async def test_a_workflow_with_no_meta_is_not_resolved_and_is_retried_next_time(
    tmp_path: Path,
) -> None:
    project = tmp_path / "p"
    flaky = _write(project / CONFIG_DIR / "workflows" / "flaky.mjs", "FAIL")
    disc, runtime = _discovery(project)

    os.utime(flaky, (1_900_000_000, 1_900_000_000))

    assert await disc.resolve("flaky") is None
    flaky.write_text("flaky-fixed")
    # the same mtime again: a cache hit if the failure had been cached
    os.utime(flaky, (1_900_000_000, 1_900_000_000))
    again = await disc.resolve("flaky")

    assert again is not None and again.meta.name == "flaky-fixed"
    assert len(runtime.probed) == 2


async def test_a_second_listing_probes_nothing_until_the_file_changes(tmp_path: Path) -> None:
    project = tmp_path / "p"
    wf = _write(project / CONFIG_DIR / "workflows" / "a.mjs", "v1")
    disc, runtime = _discovery(project)

    first = await disc.list_workflows()
    await disc.list_workflows()
    assert len(runtime.probed) == 1

    wf.write_text("v2")
    os.utime(wf, (1_900_000_000, 1_900_000_000))
    changed = await disc.list_workflows()

    assert [e.meta.name for e in first] == ["v1"]
    assert [e.meta.name for e in changed] == ["v2"]
    assert len(runtime.probed) == 2


async def test_no_workflow_directories_gives_an_empty_list_and_probes_nothing(
    tmp_path: Path,
) -> None:
    disc, runtime = _discovery(tmp_path / "p")

    assert await disc.list_workflows() == []
    assert await disc.resolve("anything") is None
    assert runtime.probed == []


async def test_resolve_picks_the_winning_copy_of_a_name(tmp_path: Path) -> None:
    project, home = tmp_path / "p", tmp_path / "h"
    _write(home / CONFIG_DIR / "workflows" / "shared.mjs", "user")
    _write(project / ".claude" / "workflows" / "shared.mjs", "claude")
    disc, _ = _discovery(project, home=home)

    found = await disc.resolve("shared")

    assert found is not None and found.meta.name == "claude"
    assert found.path == ".claude/workflows/shared.mjs"


async def test_an_untrusted_folder_s_workflows_are_neither_listed_nor_probed(
    tmp_path: Path,
) -> None:
    project, home = tmp_path / "p", tmp_path / "h"
    _write(project / CONFIG_DIR / "workflows" / "evil.mjs", "evil")
    _write(home / CONFIG_DIR / "workflows" / "mine.mjs", "mine")
    disc, runtime = _discovery(project, home=home, trusted=False)

    listed = await disc.list_workflows()

    assert [e.meta.name for e in listed] == ["mine"]
    assert [p.name for p in runtime.probed] == ["mine.mjs"]
    assert await disc.resolve("evil") is None


async def test_the_trust_question_is_about_the_project_and_its_workflows(tmp_path: Path) -> None:
    project = tmp_path / "p"
    disc, _ = _discovery(project)

    disc.paths()

    assert disc._trust.asked == [(project, "workflows")]  # type: ignore[attr-defined]


async def test_refusing_a_missing_workflow_is_a_not_found_result_that_says_why(
    tmp_path: Path,
) -> None:
    project = tmp_path / "p"
    trusted, _ = _discovery(project, trusted=True)
    untrusted, _ = _discovery(project, trusted=False)

    refusal = trusted.refuse_missing("nope")
    untrusted_refusal = untrusted.refuse_missing("nope")

    assert refusal.ok is False
    assert refusal.failure == "not_found"
    assert refusal.reason == trusted.not_found_message("nope")
    assert "workflow 'nope' not found in" in refusal.reason
    assert "not trusted" not in refusal.reason
    assert untrusted_refusal.failure == "not_found"
    assert "this folder is not trusted" in untrusted_refusal.reason


async def test_a_workflow_that_vanishes_between_listing_and_reading_is_skipped(
    tmp_path: Path,
) -> None:
    project = tmp_path / "p"
    keep = _write(project / CONFIG_DIR / "workflows" / "keep.mjs", "keep")
    gone = _write(project / CONFIG_DIR / "workflows" / "gone.mjs", "gone")
    disc, _ = _discovery(project)
    real_paths = disc.paths

    def paths_then_delete() -> list[Path]:
        found = real_paths()
        gone.unlink()
        return found

    disc.paths = paths_then_delete  # type: ignore[method-assign]

    listed = await disc.list_workflows()

    assert [e.meta.name for e in listed] == ["keep"]
    assert keep.exists()
