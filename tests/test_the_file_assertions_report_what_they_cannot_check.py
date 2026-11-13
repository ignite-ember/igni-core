"""File assertion corners: directories, regex semantics, paths, unreadable input."""

import pytest
from pydantic import ValidationError

from igni_core.core.evals.assertions import (
    FILE_ASSERTION,
    FileCheckResult,
    FileContains,
    FileExists,
    FileNotContains,
    FileNotExists,
)


class TestDirectories:
    def test_a_directory_is_not_a_file_that_exists(self, tmp_path):
        check = FileExists(path=str(tmp_path)).check()
        assert check.passed is False
        assert check.detail == f"{tmp_path} does not exist"

    def test_a_directory_counts_as_existing_for_not_exists(self, tmp_path):
        check = FileNotExists(path=str(tmp_path)).check()
        assert check.passed is False
        assert check.detail == f"{tmp_path} exists (expected not to)"

    def test_contains_on_a_directory_fails_as_missing(self, tmp_path):
        assert FileContains(path=str(tmp_path), pattern="x").check().passed is False


class TestDetails:
    def test_each_kind_names_its_path_and_pattern(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("alpha")
        assert FileExists(path=str(f)).check().detail == f"{f} exists"
        assert FileContains(path=str(f), pattern="al").check().detail == f"{f} contains /al/"
        assert (
            FileContains(path=str(f), pattern="zz").check().detail == f"{f} does not contain /zz/"
        )
        assert (
            FileNotContains(path=str(f), pattern="al").check().detail
            == f"{f} contains /al/ (expected not to)"
        )
        assert (
            FileNotContains(path=str(f), pattern="zz").check().detail
            == f"{f} does not contain /zz/"
        )

    def test_not_contains_passes_for_a_missing_file(self, tmp_path):
        f = tmp_path / "gone.txt"
        check = FileNotContains(path=str(f), pattern="x").check()
        assert check.passed is True
        assert check.detail == f"{f} does not exist (OK for not_contains)"
        assert check.type == "file_not_contains"


class TestPatternSemantics:
    def test_the_empty_default_pattern_matches_any_file(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("")
        assert FileContains(path=str(f)).check().passed is True
        assert FileNotContains(path=str(f)).check().passed is False

    def test_the_pattern_is_a_regex_searched_anywhere(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("one\ntwo 42\n")
        assert FileContains(path=str(f), pattern=r"two \d+").check().passed is True

    def test_caret_only_anchors_the_start_of_the_text(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("one\ntwo\n")
        assert FileContains(path=str(f), pattern="^two").check().passed is False

    def test_unicode_content_is_matched(self, tmp_path):
        f = tmp_path / "ü.txt"
        f.write_text("grüße", encoding="utf-8")
        assert FileContains(path=str(f), pattern="grüße").check().passed is True


class TestPathResolution:
    def test_an_absolute_path_ignores_the_work_dir(self, tmp_path):
        f = tmp_path / "abs.txt"
        f.write_text("x")
        other = tmp_path / "elsewhere"
        other.mkdir()
        assert FileExists(path=str(f)).check(other).passed is True

    def test_a_relative_path_without_a_work_dir_is_used_as_given(self, tmp_path, monkeypatch):
        (tmp_path / "rel.txt").write_text("x")
        monkeypatch.chdir(tmp_path)
        assert FileExists(path="rel.txt").check(None).passed is True

    def test_a_relative_path_is_looked_up_in_the_work_dir_not_the_cwd(self, tmp_path, monkeypatch):
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / "only_here.txt").write_text("x")
        monkeypatch.chdir(tmp_path)
        assert FileExists(path="only_here.txt").check(sandbox).passed is True
        assert FileExists(path="only_here.txt").check(None).passed is False

    def test_an_empty_path_resolves_to_the_work_dir_itself(self, tmp_path):
        assert FileNotExists(path="").check(tmp_path).passed is False


class TestWhatCannotBeChecked:
    @pytest.mark.parametrize("kind", [FileContains, FileNotContains])
    def test_an_invalid_regex_fails_with_a_message(self, tmp_path, kind):
        f = tmp_path / "a.txt"
        f.write_text("text")
        check = kind(path=str(f), pattern="(unclosed").check()
        assert check.passed is False
        assert check.detail.startswith(f"{f} could not be checked:")

    @pytest.mark.parametrize("kind", [FileContains, FileNotContains])
    def test_a_file_that_is_not_text_fails_with_a_message(self, tmp_path, kind):
        f = tmp_path / "blob.bin"
        f.write_bytes(b"\xff\xfe\x00\x80binary")
        check = kind(path=str(f), pattern="binary").check()
        assert check.passed is False
        assert "could not be checked" in check.detail


class TestModels:
    def test_a_result_forbids_extra_fields(self):
        with pytest.raises(ValidationError):
            FileCheckResult(type="t", passed=True, detail="d", extra=1)

    def test_every_kind_validates_by_its_type_name(self):
        kinds = {
            "file_exists": FileExists,
            "file_not_exists": FileNotExists,
            "file_contains": FileContains,
            "file_not_contains": FileNotContains,
        }
        for name, cls in kinds.items():
            assert type(FILE_ASSERTION.validate_python({"type": name, "path": "p"})) is cls

    def test_a_missing_type_is_refused(self):
        with pytest.raises(ValidationError):
            FILE_ASSERTION.validate_python({"path": "p"})
