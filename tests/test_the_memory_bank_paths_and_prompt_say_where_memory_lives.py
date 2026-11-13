"""Where a project's memory lives, what its index says, what the agent is told.

The bank runs against real directories; the cloud case runs against an
in-memory :class:`MemoryFiles`, the contract the database backend meets.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from igni_core.core.paths import CONFIG_DIR
from igni_core.core.utils import context_managed
from igni_core.core.utils.context_memory import ProjectMemoryBank
from igni_core.core.utils.context_memory_paths import ProjectMemoryPaths
from igni_core.core.utils.context_memory_prompt import (
    MEMORY_WRITEBACK_TEMPLATE,
    MemoryWritebackPrompt,
)
from igni_core.core.utils.context_readers import read_rules_dir, rules_filenames
from igni_core.core.utils.memory_files import (
    DirectoryMemoryFiles,
    MemoryFiles,
    MemoryMirror,
)


class _Dirs:
    def __init__(self, root: Path) -> None:
        self.igni = root / "igni_mem"
        self.claude = root / "claude_mem"

    def igni_dir(self) -> Path:
        return self.igni

    def claude_dir(self) -> Path:
        return self.claude


class _Rows(MemoryFiles):
    """The cloud's memory files: named rows, no directory."""

    def __init__(self, rows: dict[str, str] | None = None) -> None:
        self.rows = dict(rows or {})

    def read(self, name: str) -> str | None:
        return self.rows.get(name)

    def write(self, name: str, content: str) -> None:
        self.rows[name] = content

    def names(self) -> list[str]:
        return sorted(self.rows)

    def delete(self, name: str) -> bool:
        return self.rows.pop(name, None) is not None


# ── Paths ──────────────────────────────────────────────────────────


def test_a_symlinked_project_slugs_as_the_real_directory(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert ProjectMemoryPaths(link).slug == ProjectMemoryPaths(real).slug


def test_dashes_spaces_and_unicode_in_a_path_are_kept_in_the_slug():
    slug = ProjectMemoryPaths(Path("/Users/ünï/my proj/a-b")).slug
    assert slug == "-Users-ünï-my proj-a-b"


def test_the_directories_follow_the_home_at_the_time_of_the_call(tmp_path, monkeypatch):
    paths = ProjectMemoryPaths(Path("/Users/x/proj"))
    for name in ("one", "two"):
        monkeypatch.setattr(Path, "home", lambda name=name: tmp_path / name)
        assert (
            paths.igni_dir()
            == tmp_path / name / CONFIG_DIR / "projects" / "-Users-x-proj" / "memory"
        )
        assert (
            paths.claude_dir()
            == tmp_path / name / ".claude" / "projects" / "-Users-x-proj" / "memory"
        )


# ── Managed policy ─────────────────────────────────────────────────


def test_a_managed_directory_that_cannot_be_inspected_is_no_policy(monkeypatch, tmp_path):
    (tmp_path / "igni.md").write_text("POLICY")
    monkeypatch.setattr(context_managed, "platform_managed_rules_dir", lambda: tmp_path)

    def boom(self):
        raise OSError("stale handle")

    monkeypatch.setattr(Path, "is_dir", boom)
    assert context_managed.load_managed_rules(read_rules_dir, rules_filenames) == ""


def test_the_managed_directory_is_the_platform_directory_the_paths_module_names(monkeypatch):
    sentinel = Path("/somewhere/policy")
    monkeypatch.setattr(context_managed, "managed_policy_dir", lambda: sentinel)
    assert context_managed.platform_managed_rules_dir() == sentinel


# ── The prompt ─────────────────────────────────────────────────────


def test_the_template_has_one_placeholder_and_it_is_the_memory_directory():
    assert MEMORY_WRITEBACK_TEMPLATE.count("$memory_dir") >= 1
    assert MemoryWritebackPrompt.TEMPLATE.get_identifiers() == ["memory_dir"]


def test_a_render_substitutes_every_placeholder_and_keeps_the_literal_braces(tmp_path):
    out = MemoryWritebackPrompt(tmp_path / "mem").render()
    assert "$memory_dir" not in out
    assert str(tmp_path / "mem") in out
    assert "{memory body" in out


@pytest.mark.parametrize("name", ["with space", "ünï", "has$dollar", "$memory_dir"])
def test_a_directory_name_is_substituted_as_data(tmp_path, name):
    target = tmp_path / name
    out = MemoryWritebackPrompt(target).render()
    assert str(target) in out
    assert out == MEMORY_WRITEBACK_TEMPLATE.replace("$memory_dir", str(target))


def test_the_banks_instructions_are_the_prompt_for_its_own_igni_dir(tmp_path):
    bank = ProjectMemoryBank(tmp_path / "proj", paths=_Dirs(tmp_path))
    assert bank.writeback_instructions() == MemoryWritebackPrompt(tmp_path / "igni_mem").render()


# ── The bank on the desktop ────────────────────────────────────────


def test_the_bank_hands_back_what_it_was_given(tmp_path):
    bank = ProjectMemoryBank(tmp_path, read_claude_fallback=False)
    assert bank.project_dir == tmp_path
    assert bank.read_claude_fallback is False
    assert ProjectMemoryBank(tmp_path).read_claude_fallback is True


def _write_index(directory: Path, text: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "MEMORY.md").write_text(text)


def test_exactly_two_hundred_lines_survive_and_the_two_hundred_and_first_does_not(tmp_path):
    paths = _Dirs(tmp_path)
    exact = "".join(f"l{i}\n" for i in range(200))
    _write_index(paths.igni, exact)
    bank = ProjectMemoryBank(tmp_path, paths=paths)
    assert bank.load_index() == exact
    _write_index(paths.igni, exact + "l200\n")
    assert bank.load_index() == exact


def test_an_index_without_a_final_newline_keeps_its_last_line_and_crlf_counts_as_one_line_break(
    tmp_path,
):
    paths = _Dirs(tmp_path)
    (paths.igni).mkdir()
    (paths.igni / "MEMORY.md").write_bytes(b"a\r\nb")
    assert ProjectMemoryBank(tmp_path, paths=paths).load_index() == "a\nb"


def test_the_byte_cap_is_twenty_five_thousand_bytes_exactly(tmp_path):
    assert ProjectMemoryBank.cap_index("x" * 25_000) == "x" * 25_000
    assert ProjectMemoryBank.cap_index("x" * 25_001) == "x" * 25_000


def test_a_character_split_by_the_byte_cap_is_dropped_whole(tmp_path):
    out = ProjectMemoryBank.cap_index("x" * 24_999 + "é")
    assert out == "x" * 24_999
    out.encode("utf-8")


def test_an_empty_igni_index_falls_through_to_the_claude_one(tmp_path):
    paths = _Dirs(tmp_path)
    _write_index(paths.igni, "")
    _write_index(paths.claude, "claude index")
    assert ProjectMemoryBank(tmp_path, paths=paths).load_index() == "claude index"
    assert ProjectMemoryBank(tmp_path, read_claude_fallback=False, paths=paths).load_index() == ""


def test_the_files_are_resolved_once_and_kept(tmp_path, monkeypatch):
    calls: list[Path] = []
    rows = _Rows()

    def for_project(project_dir, directory, locator=None):
        calls.append(directory)
        return rows

    monkeypatch.setattr(MemoryFiles, "for_project", staticmethod(for_project))
    bank = ProjectMemoryBank(tmp_path, paths=_Dirs(tmp_path))
    assert bank.files() is rows
    assert bank.files() is rows
    assert calls == [tmp_path / "igni_mem"]


def test_on_the_desktop_the_files_are_the_igni_directory_and_there_is_no_mirror(tmp_path):
    bank = ProjectMemoryBank(tmp_path, paths=_Dirs(tmp_path))
    files = bank.files()
    assert isinstance(files, DirectoryMemoryFiles)
    assert files.directory == tmp_path / "igni_mem"
    assert bank.mirror() is None


# ── The bank in the cloud ──────────────────────────────────────────


def test_in_the_cloud_the_index_is_the_row_and_the_claude_directory_is_not_read(tmp_path):
    paths = _Dirs(tmp_path)
    _write_index(paths.claude, "claude index")
    bank = ProjectMemoryBank(tmp_path, files=_Rows(), paths=paths)
    assert bank.load_index() == ""
    bank = ProjectMemoryBank(tmp_path, files=_Rows({"MEMORY.md": "shared index"}), paths=paths)
    assert bank.load_index() == "shared index"


def test_in_the_cloud_the_index_is_capped_like_a_file(tmp_path):
    rows = _Rows({"MEMORY.md": "".join(f"r{i}\n" for i in range(300))})
    out = ProjectMemoryBank(tmp_path, files=rows, paths=_Dirs(tmp_path)).load_index()
    assert out.count("\n") == 200


def test_in_the_cloud_ensure_creates_no_directory_and_returns_the_path(tmp_path):
    paths = _Dirs(tmp_path)
    bank = ProjectMemoryBank(tmp_path, files=_Rows(), paths=paths)
    assert bank.ensure() == paths.igni
    assert not paths.igni.exists()


def test_in_the_cloud_the_mirror_is_the_stored_files_written_into_the_igni_directory(tmp_path):
    paths = _Dirs(tmp_path)
    paths.igni.mkdir()
    (paths.igni / "stale.md").write_text("gone")
    bank = ProjectMemoryBank(tmp_path, files=_Rows({"MEMORY.md": "idx", "a.md": "A"}), paths=paths)
    mirror = bank.mirror()
    assert isinstance(mirror, MemoryMirror)
    assert mirror.directory == paths.igni
    assert sorted(p.name for p in paths.igni.iterdir()) == ["MEMORY.md", "a.md"]
    assert (paths.igni / "a.md").read_text() == "A"


def test_a_mirror_that_cannot_fill_is_still_returned_and_the_failure_is_logged(
    tmp_path, caplog, monkeypatch
):
    paths = _Dirs(tmp_path)
    bank = ProjectMemoryBank(tmp_path, files=_Rows({"a.md": "A"}), paths=paths)

    def boom(self):
        raise OSError("disk full")

    monkeypatch.setattr(MemoryMirror, "fill", boom)
    with caplog.at_level(logging.WARNING, logger="igni_core.core.utils.context_memory"):
        mirror = bank.mirror()
    assert isinstance(mirror, MemoryMirror)
    assert "could not fill" in caplog.text
    assert "disk full" in caplog.text
