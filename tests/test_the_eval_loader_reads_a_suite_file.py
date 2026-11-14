"""What the eval loader reads from a YAML file, what it refuses and where it looks."""

import logging
import textwrap

import pytest

from igni_core.core.evals.assertions import FileContains, FileExists
from igni_core.core.evals.loader import load_all_suites, load_eval_file
from igni_core.core.evals.schemas import FixtureSpec, ToolArgAssertion


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


class TestWhatIsRefused:
    @pytest.mark.parametrize(
        "text",
        [
            "",
            "agent: [unclosed",
            "- just\n- a list\n",
            "just a string",
            "agent: a\n",
            "cases: []\n",
            "agent: a\ncases:\n",
            "agent: a\ncases: 5\n",
        ],
    )
    def test_a_file_that_is_not_a_suite_is_skipped(self, tmp_path, text):
        assert load_eval_file(write(tmp_path / "s.yaml", text)) is None

    def test_a_missing_file_is_skipped_with_a_warning(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING):
            assert load_eval_file(tmp_path / "nope.yaml") is None
        assert "Failed to load eval file" in caplog.text

    def test_an_unknown_assertion_type_rejects_the_whole_file(self, tmp_path, caplog):
        path = write(
            tmp_path / "s.yaml",
            """\
            agent: a
            cases:
              - name: ok
                input: q
              - name: bad
                input: q
                file_assertions:
                  - {type: file_unchanged, path: x}
            """,
        )
        with caplog.at_level(logging.WARNING):
            assert load_eval_file(path) is None
        assert "failed validation" in caplog.text

    def test_a_bad_fixture_rejects_the_whole_file(self, tmp_path):
        path = write(
            tmp_path / "s.yaml",
            """\
            agent: a
            fixtures:
              - {source: 5}
            cases: [{name: c, input: q}]
            """,
        )
        assert load_eval_file(path) is None

    def test_a_case_that_is_not_a_mapping_or_lacks_a_field_is_dropped(self, tmp_path):
        path = write(
            tmp_path / "s.yaml",
            """\
            agent: a
            cases:
              - just text
              - {name: no_input}
              - {input: no_name}
              - {name: kept, input: q}
            """,
        )
        assert [c.name for c in load_eval_file(path).cases] == ["kept"]


class TestWhatIsRead:
    def test_suite_fields_and_case_fields_are_carried_over(self, tmp_path):
        path = write(
            tmp_path / "s.yaml",
            """\
            agent: editor
            description: Editor evals
            setup_module: pkg.setup
            allow_additional_tool_calls: true
            fixtures:
              - {source: a.txt, target: b.txt}
            cases:
              - name: c
                input: do it
                description: d
                expected_tool_calls: [Edit]
                unexpected_tool_calls: [Bash]
                expected_output: done
                judge_guidelines: strict
                case_timeout: 90
                prior_messages: [one, two]
            """,
        )
        suite = load_eval_file(path)
        assert (suite.agent, suite.description, suite.setup_module) == (
            "editor",
            "Editor evals",
            "pkg.setup",
        )
        assert suite.allow_additional_tool_calls is True
        assert suite.fixtures == [FixtureSpec(source="a.txt", target="b.txt")]
        case = suite.cases[0]
        assert (case.description, case.expected_tool_calls, case.unexpected_tool_calls) == (
            "d",
            ["Edit"],
            ["Bash"],
        )
        assert (case.expected_output, case.judge_guidelines, case.case_timeout) == (
            "done",
            "strict",
            90,
        )
        assert case.prior_messages == ["one", "two"]

    def test_the_optional_parts_default_to_off_and_empty(self, tmp_path):
        suite = load_eval_file(write(tmp_path / "s.yaml", "agent: a\ncases: [{name: c, input: q}]"))
        assert suite.fixtures is None
        assert suite.setup_module is None
        assert suite.allow_additional_tool_calls is False
        case = suite.cases[0]
        assert case.file_assertions is None
        assert case.tool_arg_assertions is None
        assert case.prior_messages == []
        assert case.case_timeout is None

    def test_null_prior_messages_become_an_empty_list(self, tmp_path):
        path = write(
            tmp_path / "s.yaml", "agent: a\ncases: [{name: c, input: q, prior_messages: null}]"
        )
        assert load_eval_file(path).cases[0].prior_messages == []

    def test_file_assertions_are_typed_models_at_load_time(self, tmp_path):
        path = write(
            tmp_path / "s.yaml",
            """\
            agent: a
            cases:
              - name: c
                input: q
                file_assertions:
                  - {type: file_exists, path: out.txt}
                  - {type: file_contains, path: out.txt, pattern: hi}
                  - not a mapping
            """,
        )
        first, second = load_eval_file(path).cases[0].file_assertions
        assert isinstance(first, FileExists)
        assert isinstance(second, FileContains)
        assert second.pattern == "hi"

    def test_tool_arg_assertions_are_typed_and_non_mappings_ignored(self, tmp_path):
        path = write(
            tmp_path / "s.yaml",
            """\
            agent: a
            cases:
              - name: c
                input: q
                tool_arg_assertions:
                  - {tool: spawn_team, args_must_contain: {mode: coordinate}}
                  - 7
            """,
        )
        assert load_eval_file(path).cases[0].tool_arg_assertions == [
            ToolArgAssertion(tool="spawn_team", args_must_contain={"mode": "coordinate"})
        ]

    def test_empty_assertion_and_fixture_lists_read_as_none(self, tmp_path):
        path = write(
            tmp_path / "s.yaml",
            """\
            agent: a
            fixtures: []
            cases:
              - {name: c, input: q, file_assertions: [], tool_arg_assertions: []}
            """,
        )
        suite = load_eval_file(path)
        assert suite.fixtures is None
        assert suite.cases[0].file_assertions is None
        assert suite.cases[0].tool_arg_assertions is None

    def test_unicode_survives(self, tmp_path):
        path = write(tmp_path / "s.yaml", "agent: ü\ncases: [{name: grüße, input: «q»}]")
        suite = load_eval_file(path)
        assert (suite.agent, suite.cases[0].name, suite.cases[0].input) == ("ü", "grüße", "«q»")


class TestDiscovery:
    def test_nothing_exists(self, tmp_path):
        assert load_all_suites(tmp_path) == []

    def test_both_directories_are_read_sorted_with_local_first(self, tmp_path):
        body = "agent: {}\ncases: [{{name: c, input: q}}]\n"
        write(tmp_path / ".igni" / "evals" / "b.yaml", body.format("local_b"))
        write(tmp_path / "evals" / "a.yaml", body.format("shipped_a"))
        write(tmp_path / ".igni" / "evals" / "a2.yaml", body.format("local_a2"))
        assert [s.agent for s in load_all_suites(tmp_path)] == [
            "local_a2",
            "local_b",
            "shipped_a",
        ]

    def test_a_local_file_overrides_the_shipped_one_of_the_same_name(self, tmp_path):
        body = "agent: {}\ncases: [{{name: c, input: q}}]\n"
        write(tmp_path / ".igni" / "evals" / "x.yaml", body.format("local"))
        write(tmp_path / "evals" / "x.yaml", body.format("shipped"))
        assert [s.agent for s in load_all_suites(tmp_path)] == ["local"]

    def test_an_invalid_local_file_still_shadows_the_shipped_one(self, tmp_path):
        write(tmp_path / ".igni" / "evals" / "x.yaml", "not: a suite")
        write(tmp_path / "evals" / "x.yaml", "agent: shipped\ncases: [{name: c, input: q}]")
        assert load_all_suites(tmp_path) == []

    def test_only_yaml_files_are_discovered(self, tmp_path):
        body = "agent: a\ncases: [{name: c, input: q}]\n"
        write(tmp_path / "evals" / "a.yml", body)
        write(tmp_path / "evals" / "a.txt", body)
        assert load_all_suites(tmp_path) == []

    def test_invalid_files_are_omitted(self, tmp_path):
        write(tmp_path / "evals" / "bad.yaml", "nope")
        write(tmp_path / "evals" / "good.yaml", "agent: g\ncases: [{name: c, input: q}]")
        assert [s.agent for s in load_all_suites(tmp_path)] == ["g"]
