"""How display names in an eval YAML expand to the function names Agno calls."""

from igni_core.core.evals.tool_names import DEFAULT_CATALOG, ToolNameCatalog


class TestDefaultTable:
    def test_each_display_name_expands_to_all_its_functions(self):
        expand = DEFAULT_CATALOG.expand
        assert expand(["Write"]) == ["save_file", "create_file"]
        assert expand(["Edit"]) == ["edit_file", "edit_file_replace_all"]
        assert expand(["Bash"]) == [
            "run_shell_command",
            "read_process_output",
            "watch_process",
            "stop_process",
            "list_processes",
        ]
        assert expand(["WebSearch"]) == ["web_search", "search_news"]
        assert expand(["WebFetch"]) == ["fetch_url", "fetch_json"]

    def test_schedule_expands_to_all_three_schedule_functions(self):
        assert DEFAULT_CATALOG.expand(["Schedule"]) == [
            "schedule_task",
            "list_scheduled_tasks",
            "cancel_scheduled_task",
        ]

    def test_the_removed_read_family_passes_through_verbatim(self):
        assert DEFAULT_CATALOG.expand(["Read", "Grep", "Glob", "LS"]) == [
            "Read",
            "Grep",
            "Glob",
            "LS",
        ]

    def test_display_names_are_case_sensitive(self):
        assert DEFAULT_CATALOG.expand(["write"]) == ["write"]


class TestExpand:
    def test_unknown_names_pass_through_in_order(self):
        assert DEFAULT_CATALOG.expand(["spawn_agent", "Write", "spawn_team"]) == [
            "spawn_agent",
            "save_file",
            "create_file",
            "spawn_team",
        ]

    def test_duplicates_collapse_keeping_first_position(self):
        assert DEFAULT_CATALOG.expand(["Write", "x", "Write", "save_file", "x"]) == [
            "save_file",
            "create_file",
            "x",
        ]

    def test_empty_stays_empty(self):
        assert DEFAULT_CATALOG.expand([]) == []

    def test_expand_adds_no_system_tool(self):
        assert "update_user_memory" not in DEFAULT_CATALOG.expand(["Write"])


class TestExpandExpected:
    def test_the_always_allowed_tool_is_appended_once(self):
        assert DEFAULT_CATALOG.expand_expected(["Write"]) == [
            "save_file",
            "create_file",
            "update_user_memory",
        ]
        assert DEFAULT_CATALOG.expand_expected(["update_user_memory", "Write"]) == [
            "update_user_memory",
            "save_file",
            "create_file",
        ]

    def test_empty_expected_still_allows_the_system_tool(self):
        assert DEFAULT_CATALOG.expand_expected([]) == ["update_user_memory"]


class TestOverrides:
    def test_constructor_overrides_both_tables(self):
        cat = ToolNameCatalog({"Go": ["a", "b"]}, ("sys",))
        assert cat.expand_expected(["Go", "Write"]) == ["a", "b", "Write", "sys"]

    def test_an_empty_expansion_table_passes_everything_through(self):
        assert ToolNameCatalog({}).expand(["Write"]) == ["Write"]

    def test_the_table_is_copied_so_later_edits_do_not_leak_in(self):
        table = {"Go": ["a"]}
        cat = ToolNameCatalog(table)
        table["Go"] = ["z"]
        table["New"] = ["n"]
        assert cat.expand(["New"]) == ["New"]

    def test_defaults_are_the_class_tables_and_the_default_catalog_is_shared(self):
        cat = ToolNameCatalog()
        assert cat.expansions == ToolNameCatalog.DEFAULT_EXPANSIONS
        assert cat.expansions is not ToolNameCatalog.DEFAULT_EXPANSIONS
        assert cat.always_allowed_system_tools == ("update_user_memory",)
