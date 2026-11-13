"""The user tier's three sources: where they are read from, in what order,
what is left out, and the value that names the locations."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from igni_core.core.utils import context_user
from igni_core.core.utils.context import RulesContextLoader
from igni_core.core.utils.context_schemas import (
    RulesBundle,
    RulesSection,
    SubdirectoryRules,
    UserRulesPaths,
)
from igni_core.core.utils.context_user import default_user_rules_paths, load_user_rules


def _paths(root: Path) -> UserRulesPaths:
    return UserRulesPaths(
        legacy_file=root / "igni" / "rules.md",
        rules_dir=root / "igni" / "rules",
        claude_rules_dir=root / "claude" / "rules",
    )


class _Readers:
    """The two shared readers the loader injects, answering from a table."""

    def __init__(self, answers: dict[Path, str]) -> None:
        self.answers = answers
        self.file_calls: list[tuple[Path, Path]] = []
        self.dir_calls: list[tuple[Path, Path | None, Path | None]] = []

    def read_with_imports(self, path: Path, allowed_root: Path) -> str:
        self.file_calls.append((path, allowed_root))
        return self.answers.get(path, "")

    def read_dir(
        self, directory: Path, working_dir: Path | None = None, project_dir: Path | None = None
    ) -> str:
        self.dir_calls.append((directory, working_dir, project_dir))
        return self.answers.get(directory, "")


def _load(readers: _Readers, paths: UserRulesPaths, **kwargs) -> str:
    return load_user_rules(readers.read_with_imports, readers.read_dir, paths, **kwargs)


def test_the_three_sources_are_joined_in_order_by_a_blank_line(tmp_path: Path):
    paths = _paths(tmp_path)
    readers = _Readers(
        {paths.legacy_file: "LEGACY", paths.rules_dir: "DIR", paths.claude_rules_dir: "CLAUDE"}
    )

    assert _load(readers, paths) == "LEGACY\n\nDIR\n\nCLAUDE"


def test_a_source_that_reads_empty_is_left_out_with_no_stray_separator(tmp_path: Path):
    paths = _paths(tmp_path)

    assert _load(_Readers({paths.rules_dir: "DIR"}), paths) == "DIR"
    assert _load(_Readers({paths.legacy_file: "A", paths.claude_rules_dir: "C"}), paths) == "A\n\nC"


def test_with_no_source_the_rules_are_empty(tmp_path: Path):
    assert _load(_Readers({}), _paths(tmp_path)) == ""


def test_the_legacy_file_may_import_only_from_its_own_directory(tmp_path: Path):
    paths = _paths(tmp_path)
    readers = _Readers({})

    _load(readers, paths)

    assert readers.file_calls == [(paths.legacy_file, paths.legacy_file.parent)]


def test_the_working_and_project_directories_reach_both_directory_readers(tmp_path: Path):
    paths = _paths(tmp_path)
    readers = _Readers({})
    work, project = tmp_path / "w", tmp_path / "p"

    _load(readers, paths, working_dir=work, project_dir=project)

    assert readers.dir_calls == [
        (paths.rules_dir, work, project),
        (paths.claude_rules_dir, work, project),
    ]


def test_without_the_cross_tool_switch_the_claude_directory_is_not_read(tmp_path: Path):
    paths = _paths(tmp_path)
    readers = _Readers({paths.claude_rules_dir: "CLAUDE", paths.rules_dir: "DIR"})

    assert _load(readers, paths, read_claude_rules=False) == "DIR"
    assert [d for d, _, _ in readers.dir_calls] == [paths.rules_dir]


def test_the_loader_reads_the_locations_it_is_given_not_the_module_constants(
    tmp_path: Path, monkeypatch
):
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setattr(context_user, "USER_RULES_PATH", elsewhere / "rules.md")
    monkeypatch.setattr(context_user, "USER_RULES_DIR", elsewhere / "rules")
    monkeypatch.setattr(context_user, "CLAUDE_USER_RULES_DIR", elsewhere / "claude")
    (elsewhere).mkdir()
    (elsewhere / "rules.md").write_text("FROM THE CONSTANTS")
    paths = _paths(tmp_path / "given")
    paths.legacy_file.parent.mkdir(parents=True)
    paths.legacy_file.write_text("FROM THE GIVEN FILE")
    paths.rules_dir.mkdir()
    (paths.rules_dir / "topic.md").write_text("FROM THE GIVEN DIR")

    text = RulesContextLoader(project_dir=tmp_path / "proj", user_rules_paths=paths).load_user()

    assert "FROM THE GIVEN FILE" in text
    assert "FROM THE GIVEN DIR" in text
    assert "FROM THE CONSTANTS" not in text


def test_the_default_locations_are_the_three_module_constants():
    paths = default_user_rules_paths()

    assert paths.legacy_file == context_user.USER_RULES_PATH
    assert paths.rules_dir == context_user.USER_RULES_DIR
    assert paths.claude_rules_dir == context_user.CLAUDE_USER_RULES_DIR
    assert paths.legacy_file.name == "rules.md"
    assert paths.rules_dir.name == "rules"
    assert paths.claude_rules_dir.parent.name == ".claude"


def test_the_default_locations_are_taken_when_the_loader_is_built(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(context_user, "USER_RULES_PATH", tmp_path / "later.md")

    assert (
        RulesContextLoader(project_dir=tmp_path).user_rules_paths.legacy_file
        == tmp_path / "later.md"
    )


def test_the_module_prose_names_the_product_igni():
    assert "ember" not in (context_user.__doc__ or "").lower()
    assert "ember" not in (load_user_rules.__doc__ or "").lower()


# ── The schemas ─────────────────────────────────────────────


def test_a_section_renders_its_heading_a_blank_line_and_its_body():
    assert RulesSection(heading="User Rules", body="be kind").render() == "# User Rules\n\nbe kind"


def test_an_empty_section_still_renders_its_heading():
    assert RulesSection(heading="h", body="").render() == "# h\n\n"


def test_a_body_with_a_divider_in_it_is_not_altered():
    assert RulesSection(heading="h", body="a\n---\nb").render() == "# h\n\na\n---\nb"


def test_a_directory_entry_becomes_a_section_headed_by_its_path():
    section = SubdirectoryRules(rel_path="src/auth", content="no globals").to_section()

    assert section == RulesSection(heading="Directory Rules (src/auth/)", body="no globals")
    assert section.render() == "# Directory Rules (src/auth/)\n\nno globals"


def test_a_bundle_joins_its_sections_with_exactly_the_divider():
    bundle = RulesBundle(
        sections=[RulesSection(heading="a", body="1"), RulesSection(heading="b", body="2")]
    )

    assert bundle.render() == "# a\n\n1\n\n---\n\n# b\n\n2"


def test_a_bundle_of_one_section_has_no_divider():
    assert RulesBundle(sections=[RulesSection(heading="a", body="1")]).render() == "# a\n\n1"


def test_a_bundle_with_no_section_renders_nothing():
    assert RulesBundle(sections=[]).render() == ""


@pytest.mark.parametrize(
    "value",
    [
        RulesSection(heading="h", body="b"),
        SubdirectoryRules(rel_path="p", content="c"),
        UserRulesPaths(legacy_file=Path("a"), rules_dir=Path("b"), claude_rules_dir=Path("c")),
        RulesBundle(sections=[]),
    ],
)
def test_the_values_are_frozen(value):
    field = next(iter(type(value).model_fields))

    with pytest.raises(ValidationError):
        setattr(value, field, getattr(value, field))


def test_the_locations_need_all_three_paths():
    with pytest.raises(ValidationError):
        UserRulesPaths(legacy_file=Path("a"), rules_dir=Path("b"))  # type: ignore[call-arg]
