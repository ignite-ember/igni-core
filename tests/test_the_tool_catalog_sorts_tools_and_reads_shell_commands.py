"""What the permission catalog calls an edit, a read and a shell, and how it reads a command.

Plan mode blocks edits and auto-allows web reads; acceptEdits auto-approves
edits. Which tool names count is the catalog's whole job, so each name is
pinned here by the decision a real evaluator makes for it — not by the set
it is stored in. The shell half is the mutation heuristic and the shapes a
shell call's arguments arrive in.
"""

from __future__ import annotations

import pytest

from igni_core.core.config.permission_eval import (
    FILE_EDIT_TOOLS,
    FILE_READ_TOOLS,
    SHELL_TOOLS,
    BashCommand,
    PermissionDecision,
    PermissionEvaluator,
    ToolCategoryCatalog,
)
from igni_core.core.mcp.hints import ToolHints, ToolHintsRegistry

_EDITS = [
    "Edit",
    "Write",
    "NotebookEdit",
    "save_file",
    "edit_file",
    "edit_file_replace_all",
    "create_file",
    "notebook_edit_cell",
    "notebook_add_cell",
    "notebook_remove_cell",
]
_READS = [
    "WebSearch",
    "web_search",
    "search_news",
    "duckduckgo_search",
    "duckduckgo_news",
    "WebFetch",
    "fetch_url",
    "fetch_json",
]


def _decide(mode: str, tool: str, args: dict | None = None, **kw) -> PermissionDecision:
    return PermissionEvaluator.from_strings(mode=mode, **kw).evaluate(tool, args or {})


class TestWhichNamesAreEdits:
    def test_the_edit_set_is_exactly_these_names(self):
        assert frozenset(_EDITS) == FILE_EDIT_TOOLS

    @pytest.mark.parametrize("tool", _EDITS)
    def test_plan_mode_blocks_each_edit(self, tool):
        assert _decide("plan", tool) is PermissionDecision.DENY

    @pytest.mark.parametrize("tool", _EDITS)
    def test_accept_edits_approves_each_edit(self, tool):
        assert _decide("acceptEdits", tool) is PermissionDecision.ALLOW

    @pytest.mark.parametrize(
        "tool", ["notebook_edit_cell", "notebook_add_cell", "notebook_remove_cell"]
    )
    def test_the_notebook_writers_the_hook_sees_are_blocked_not_deferred(self, tool):
        """The evaluator is handed the function name; 'NotebookEdit' alone matched nothing."""
        assert _decide("plan", tool) is not PermissionDecision.DEFER
        assert ToolCategoryCatalog.default().is_edit(tool)


class TestWhichNamesAreReads:
    def test_the_read_set_is_exactly_these_names(self):
        assert frozenset(_READS) == FILE_READ_TOOLS

    @pytest.mark.parametrize("tool", _READS)
    def test_plan_mode_allows_each_web_read(self, tool):
        assert _decide("plan", tool) is PermissionDecision.ALLOW

    @pytest.mark.parametrize("tool", ["web_search", "search_news"])
    def test_the_functions_the_toolkit_really_registers_are_allowed(self, tool):
        assert ToolCategoryCatalog.default().is_read(tool)

    @pytest.mark.parametrize("tool", ["Read", "Grep", "Glob", "LS", "read_file", "list_files"])
    def test_file_reading_tools_are_not_auto_allowed_in_plan_mode(self, tool):
        """Reading in plan mode goes through read-only Bash, not an auto-allow."""
        assert not ToolCategoryCatalog.default().is_read(tool)
        assert _decide("plan", tool) is not PermissionDecision.ALLOW


class TestWhichNamesAreShells:
    def test_the_shell_set_is_exactly_these_names(self):
        assert frozenset({"Bash", "run_shell_command"}) == SHELL_TOOLS

    def test_membership_is_by_exact_name(self):
        catalog = ToolCategoryCatalog.default()

        assert catalog.is_shell("Bash") and catalog.is_shell("run_shell_command")
        assert not catalog.is_shell("bash")
        assert not catalog.is_shell("edit_file")
        assert not catalog.is_edit("Bash")
        assert not catalog.is_read("Bash")


class TestACatalogWithItsOwnSets:
    def test_custom_sets_replace_the_defaults(self):
        catalog = ToolCategoryCatalog(
            file_edit_tools=frozenset({"my_writer"}),
            file_read_tools=frozenset({"my_reader"}),
            shell_tools=frozenset({"my_shell"}),
        )

        assert catalog.is_edit("my_writer") and not catalog.is_edit("edit_file")
        assert catalog.is_read("my_reader") and not catalog.is_read("fetch_url")
        assert catalog.is_shell("my_shell") and not catalog.is_shell("Bash")

    def test_an_evaluator_classifies_with_the_catalog_it_is_given(self):
        catalog = ToolCategoryCatalog(file_edit_tools=frozenset({"my_writer"}))
        evaluator = PermissionEvaluator.from_strings(mode="plan", catalog=catalog)

        assert evaluator.evaluate("my_writer", {}) is PermissionDecision.DENY
        assert PermissionEvaluator.from_strings(mode="plan").evaluate("my_writer", {}) is (
            PermissionDecision.DEFER
        )

    def test_empty_sets_classify_nothing(self):
        catalog = ToolCategoryCatalog(
            file_edit_tools=frozenset(), file_read_tools=frozenset(), shell_tools=frozenset()
        )

        assert not any(
            catalog.is_edit(t) or catalog.is_read(t) or catalog.is_shell(t)
            for t in _EDITS + _READS + ["Bash"]
        )

    def test_default_is_a_fresh_catalog_on_the_module_sets(self):
        first, second = ToolCategoryCatalog.default(), ToolCategoryCatalog.default()

        assert first is not second
        assert first.file_edit_tools == FILE_EDIT_TOOLS
        assert first.file_read_tools == FILE_READ_TOOLS
        assert first.shell_tools == SHELL_TOOLS

    def test_the_default_reads_the_process_wide_hints_registry(self):
        assert ToolCategoryCatalog.default().tool_hints is ToolHintsRegistry.shared()


class TestAnExtensionToolIsClassifiedByItsManifest:
    READ = ToolHints(read_only=True, destructive=False, idempotent=True, open_world=False)
    WRITE = ToolHints(read_only=False, destructive=False, idempotent=False, open_world=False)

    def _catalog(self) -> ToolCategoryCatalog:
        registry = ToolHintsRegistry()
        registry.declare("ext:t", {"mcp_ext:t.look": self.READ, "mcp_ext:t.put": self.WRITE})
        return ToolCategoryCatalog(tool_hints=registry)

    def test_hints_are_the_manifests_for_a_registered_tool_and_none_for_igni_s_own(self):
        catalog = self._catalog()

        assert catalog.hints("mcp_ext:t.look") == self.READ
        assert catalog.hints("edit_file") is None
        assert catalog.hints("mcp_ext:t.unknown") is None

    def test_plan_mode_runs_a_declared_read_and_blocks_a_declared_write_by_hint_not_name(self):
        evaluator = PermissionEvaluator.from_strings(mode="plan", catalog=self._catalog())

        assert evaluator.evaluate("mcp_ext:t.look", {}) is PermissionDecision.ALLOW
        assert evaluator.evaluate("mcp_ext:t.put", {}) is PermissionDecision.DENY

    def test_a_tool_the_manifest_never_declared_is_not_classified(self):
        evaluator = PermissionEvaluator.from_strings(mode="plan", catalog=self._catalog())

        assert evaluator.evaluate("mcp_ext:t.other", {}) is PermissionDecision.DEFER


class TestHowAShellCallIsRead:
    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            ({"command": "rm -rf x"}, "rm -rf x"),
            ({"command": ["rm", "-rf", "x"]}, "rm -rf x"),
            ({"command": ["echo", 1, None]}, "echo 1 None"),
            ({"args": ["rm", "-rf", "x"]}, "rm -rf x"),
            ({"args": "rm -rf x"}, "rm -rf x"),
            ({"command": "ls", "args": ["rm"]}, "ls"),
            ({"command": 5, "args": "rm x"}, "rm x"),
            ({"command": None, "args": ["a", "b"]}, "a b"),
            ({}, ""),
            ({"command": 5}, ""),
            ({"command": None}, ""),
            ({"args": 5}, ""),
            ({"args": {"a": 1}}, ""),
            ({"other": "rm x"}, ""),
        ],
    )
    def test_the_command_is_built_from_whichever_shape_arrives(self, args, expected):
        assert BashCommand.from_args(args).command == expected

    @pytest.mark.parametrize(
        "command",
        [
            "rm x",
            "rmdir d",
            "mv a b",
            "cp a b",
            "mkdir d",
            "touch f",
            "chmod +x f",
            "chown me f",
            "ln -s a b",
            "dd if=a of=b",
            "truncate -s 0 f",
            "echo hi | tee out",
            "ls; rm x",
            "ls && rm x",
            "ls || rm x",
            "ls | rm x",
            "ls & rm x",
            "(rm x)",
            "echo `rm x`",
            "sed -i s/a/b/ f",
            "perl -i -pe s/a/b/ f",
            "echo hi > f",
            "echo hi>f",
            "echo hi >> f",
            "echo hi >>f",
            "echo hi > /dev/null",
        ],
    )
    def test_a_command_that_writes_mutates(self, command):
        assert BashCommand(command).mutates()

    @pytest.mark.parametrize(
        "command",
        [
            "",
            "ls -la",
            "cat f | grep x",
            "pytest -q 2>&1",
            "ls 2>&1 | head",
            "cmd >& log",
            "rmx a",
            "cpython x.py",
            "echo format",
            "sed s/a/b/ f",
            "perl -pe s/a/b/ f",
            "git log --oneline",
        ],
    )
    def test_a_command_that_only_reads_does_not(self, command):
        assert not BashCommand(command).mutates()

    def test_a_very_long_or_unicode_command_is_judged_the_same_way(self):
        assert not BashCommand("echo " + "é" * 100_000).mutates()
        assert BashCommand("echo é; " + "x " * 50_000 + "; rm é").mutates()

    def test_read_only_is_the_allow_list_not_the_opposite_of_mutates(self):
        assert BashCommand("ls -la").is_read_only()
        assert not BashCommand("rm x").is_read_only()
        assert not BashCommand("").is_read_only()

    def test_the_catalog_answers_for_any_argument_shape(self):
        catalog = ToolCategoryCatalog.default()

        assert catalog.bash_mutates({"command": ["rm", "x"]})
        assert not catalog.bash_mutates({})
        assert catalog.bash_read_only({"args": "ls -la"})
        assert not catalog.bash_read_only({"args": ["rm", "x"]})
        assert not catalog.bash_read_only({"command": 5})

    def test_plan_mode_runs_a_read_command_and_blocks_a_writing_one(self):
        assert _decide("plan", "Bash", {"command": "ls -la"}) is PermissionDecision.ALLOW
        assert _decide("plan", "run_shell_command", {"command": "rm x"}) is PermissionDecision.DENY
        assert _decide("plan", "Bash", {"args": ["rm", "x"]}) is PermissionDecision.DENY
