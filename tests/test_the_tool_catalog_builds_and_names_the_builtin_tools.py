"""What the tool catalog names, how it builds each built-in toolkit and
what it refuses.

``ToolSpecCatalog`` is the single table the registry, the ephemeral
agents' ``tools:`` lists and the permission gate all read. These tests
drive the real catalog and small catalogs of their own, and read the
toolkits it builds.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agno.tools import Toolkit
from pydantic import ValidationError

from igni_core.core.tools import tool_spec
from igni_core.core.tools.edit import FileEditNotifier
from igni_core.core.tools.process_supervisor import ProcessSupervisor
from igni_core.core.tools.tool_spec import (
    BashSpec,
    EditSpec,
    NormalizeResult,
    ScheduleSpec,
    ToolBuildContext,
    ToolResolutionRequest,
    ToolResolutionResult,
    ToolSpec,
    ToolSpecCatalog,
    VisualizeSpec,
    WebFetchSpec,
    WebSearchSpec,
    WriteFileSpec,
    _UnbuiltToolkit,
)


class _Probe(Toolkit):
    """Records the keyword arguments it is built with."""

    def __init__(self, **kwargs):
        super().__init__(name="probe")
        self.kwargs = kwargs


def _context(tmp_path: Path, **kw) -> ToolBuildContext:
    return ToolBuildContext(base_dir=tmp_path, supervisor=ProcessSupervisor(), **kw)


def _probe_spec(name: str = "Probe", funcs: tuple[str, ...] = (), **kw) -> ToolSpec:
    return ToolSpec(name=name, agno_function_names=funcs, toolkit_cls=_Probe, **kw)


class TestTheDefaultCatalog:
    def test_it_holds_the_eight_built_ins_in_order(self):
        names = [spec.name for spec in ToolSpecCatalog.default().specs]

        assert names == [
            "Write",
            "Edit",
            "Bash",
            "WebSearch",
            "WebFetch",
            "Schedule",
            "NotebookEdit",
            "Visualize",
        ]

    def test_it_names_no_extension_capability(self):
        catalog = ToolSpecCatalog.default()

        assert sorted(catalog.registry_names) == sorted(
            [
                "Bash",
                "Edit",
                "NotebookEdit",
                "Schedule",
                "Visualize",
                "WebFetch",
                "WebSearch",
                "Write",
            ]
        )

    def test_registry_names_are_sorted_and_leave_the_aliases_out(self):
        names = ToolSpecCatalog.default().registry_names

        assert names == sorted(names)
        assert "BashOutput" not in names

    def test_the_names_an_agent_may_declare_add_aliases_and_the_reserved_orchestrate(self):
        names = ToolSpecCatalog.default().registry_names_with_aliases

        assert names == [
            "Bash",
            "BashOutput",
            "Edit",
            "NotebookEdit",
            "Orchestrate",
            "Schedule",
            "Visualize",
            "WebFetch",
            "WebSearch",
            "Write",
        ]

    def test_orchestrate_is_declarable_but_is_not_a_spec(self):
        catalog = ToolSpecCatalog.default()

        assert "Orchestrate" in catalog.registry_names_with_aliases
        assert "Orchestrate" not in catalog.by_name
        assert "Orchestrate" not in catalog.registry_names
        assert "Orchestrate" not in catalog.valid_ephemeral_names

    def test_an_alias_finds_the_spec_it_belongs_to(self):
        catalog = ToolSpecCatalog.default()

        assert isinstance(catalog.by_name["BashOutput"], BashSpec)
        assert catalog.by_name["BashOutput"] is catalog.by_name["Bash"]

    def test_function_names_map_to_the_registry_name_that_owns_them(self):
        table = ToolSpecCatalog.default().agno_to_registry

        assert table["save_file"] == "Write"
        assert table["run_shell_command"] == "Bash"
        assert table["web_search"] == "WebSearch"
        assert table["search_news"] == "WebSearch"
        assert table["schedule_task"] == "Schedule"

    def test_an_ephemeral_agent_may_ask_for_aliases_but_not_for_visualize(self):
        valid = ToolSpecCatalog.default().valid_ephemeral_names

        assert "BashOutput" in valid
        assert "Bash" in valid
        assert "Visualize" not in valid
        assert not VisualizeSpec().ephemeral_visible


class TestACatalogOfItsOwn:
    def test_an_empty_catalog_names_nothing(self):
        catalog = ToolSpecCatalog([])

        assert catalog.registry_names == []
        assert dict(catalog.by_name) == {}
        assert catalog.registry_names_with_aliases == ["Orchestrate"]

    def test_two_specs_claiming_one_function_are_refused_naming_both(self):
        with pytest.raises(
            ValueError,
            match=r"Duplicate agno function name 'f' claimed by specs 'A' and 'B'",
        ):
            ToolSpecCatalog([_probe_spec("A", ("f",)), _probe_spec("B", ("f",))])

    def test_register_adds_a_spec_and_refreshes_the_tables_already_read(self):
        catalog = ToolSpecCatalog([_probe_spec("A", ("fa",), aliases=("AA",))])
        assert "B" not in catalog.by_name  # warm every cached table
        assert "fb" not in catalog.agno_to_registry
        assert "B" not in catalog.valid_ephemeral_names

        catalog.register(_probe_spec("B", ("fb",)))

        assert catalog.by_name["B"].name == "B"
        assert catalog.agno_to_registry["fb"] == "B"
        assert "B" in catalog.valid_ephemeral_names
        assert catalog.registry_names == ["A", "B"]
        assert [s.name for s in catalog.specs] == ["A", "B"]

    def test_register_refuses_a_duplicate_function(self):
        catalog = ToolSpecCatalog([_probe_spec("A", ("f",))])

        with pytest.raises(ValueError, match="Duplicate agno function name 'f'"):
            catalog.register(_probe_spec("B", ("f",)))


class TestNormalize:
    def test_function_names_become_registry_names_without_repeats_in_order(self):
        result = ToolSpecCatalog.default().normalize(
            ["run_shell_command", "save_file", "Bash", "Write"]
        )

        assert result.ok is True
        assert result.names == ["Bash", "Write"]
        assert result.invalid == []

    def test_an_alias_is_kept_as_asked(self):
        assert ToolSpecCatalog.default().normalize(["BashOutput"]).names == ["BashOutput"]

    def test_mcp_names_pass_through(self):
        result = ToolSpecCatalog.default().normalize(["MCP:github", "Bash"])

        assert result.ok is True
        assert result.names == ["MCP:github", "Bash"]

    def test_unknown_names_are_reported_with_what_is_available(self):
        result = ToolSpecCatalog.default().normalize(["nope", "Bash", "zip"])

        assert result.ok is False
        assert result.invalid == ["nope", "zip"]
        assert result.available == sorted(ToolSpecCatalog.default().valid_ephemeral_names)

    def test_visualize_is_not_something_an_ephemeral_agent_may_request(self):
        assert ToolSpecCatalog.default().normalize(["Visualize"]).invalid == ["Visualize"]

    def test_the_error_message_lists_the_invalid_and_the_sorted_available(self):
        result = NormalizeResult(ok=False, invalid=["b", "a"], available=["Z", "A"])

        assert result.as_error_message() == "Unknown tool(s): b, a. Available: A, Z"


class TestResolutionModels:
    def test_a_comma_string_is_split_stripped_and_blanks_dropped(self):
        request = ToolResolutionRequest(tool_names=" Bash, ,Write ,, ")

        assert request.tool_names == ["Bash", "Write"]

    def test_a_list_is_stripped_and_blank_items_dropped(self):
        request = ToolResolutionRequest(tool_names=[" Bash ", "  ", "", "Write"])

        assert request.tool_names == ["Bash", "Write"]

    def test_nothing_given_is_an_empty_list(self):
        assert ToolResolutionRequest().tool_names == []

    @pytest.mark.parametrize("value", [3, None, ("Bash",), {"Bash"}])
    def test_anything_but_a_string_or_list_is_refused(self, value):
        with pytest.raises(TypeError, match="tool_names must be str or list"):
            ToolResolutionRequest(tool_names=value)

    def test_a_result_is_ok_exactly_when_nothing_was_unknown(self):
        assert ToolResolutionResult().ok is True
        assert ToolResolutionResult(skipped_denied=["Bash"]).ok is True
        assert ToolResolutionResult(unknown=["x"]).ok is False


class TestBuildContext:
    def test_it_is_frozen(self, tmp_path):
        context = _context(tmp_path)

        with pytest.raises(ValidationError):
            context.base_dir = tmp_path / "other"  # type: ignore[misc]

    def test_it_requires_a_base_dir_and_a_supervisor(self, tmp_path):
        with pytest.raises(ValidationError):
            ToolBuildContext(base_dir=tmp_path)  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            ToolBuildContext(supervisor=ProcessSupervisor())  # type: ignore[call-arg]

    def test_the_rest_starts_unset(self, tmp_path):
        context = _context(tmp_path)

        assert context.broadcast is None
        assert context.cloud_token is None
        assert context.cloud_server_url == ""
        assert context.file_edit_notifier is None


class TestTheKwargsASpecBuildsWith:
    def test_the_base_dir_goes_in_as_text_under_the_kwarg_that_names_it(self, tmp_path):
        built = _probe_spec(static_kwargs={"x": 1}).build(_context(tmp_path), confirm=False)

        assert built.kwargs == {"x": 1, "base_dir": str(tmp_path)}

    def test_the_base_dir_may_go_in_as_a_path(self, tmp_path):
        built = _probe_spec(base_dir_as_str=False).build(_context(tmp_path), confirm=False)

        assert built.kwargs == {"base_dir": tmp_path}
        assert isinstance(built.kwargs["base_dir"], Path)

    def test_a_spec_without_a_base_dir_kwarg_gets_none(self, tmp_path):
        built = _probe_spec(base_dir_kwarg=None).build(_context(tmp_path), confirm=False)

        assert built.kwargs == {}

    def test_the_confirm_list_goes_in_only_when_asked_and_not_empty(self, tmp_path):
        gated = _probe_spec(confirm_function_names=("f", "g"), base_dir_kwarg=None)

        assert gated.build(_context(tmp_path), confirm=True).kwargs == {
            "requires_confirmation_tools": ["f", "g"]
        }
        assert gated.build(_context(tmp_path), confirm=False).kwargs == {}
        assert _probe_spec(base_dir_kwarg=None).build(_context(tmp_path), confirm=True).kwargs == {}

    def test_the_static_kwargs_are_not_changed_by_a_build(self, tmp_path):
        spec = _probe_spec(static_kwargs={"x": 1})

        spec.build(_context(tmp_path), confirm=True)

        assert spec.static_kwargs == {"x": 1}

    def test_write_builds_only_the_save_half_of_the_file_tools(self, tmp_path):
        toolkit = WriteFileSpec().build(_context(tmp_path), confirm=True)

        assert set(toolkit.functions) == {"save_file"}
        assert toolkit.functions["save_file"].requires_confirmation is True

    def test_write_asks_nothing_when_not_confirming(self, tmp_path):
        toolkit = WriteFileSpec().build(_context(tmp_path), confirm=False)

        assert not toolkit.functions["save_file"].requires_confirmation


class TestTheBuiltInSpecsHandTheirCollaborators:
    def test_edit_hands_over_the_registrys_notifier_when_there_is_one(self, tmp_path):
        notifier = FileEditNotifier()

        toolkit = EditSpec().build(_context(tmp_path, file_edit_notifier=notifier), confirm=False)

        assert toolkit._notifier is notifier

    def test_edit_without_a_notifier_falls_back_to_the_shared_default(self, tmp_path):
        from igni_core.core.tools.edit import default_file_edit_notifier

        toolkit = EditSpec().build(_context(tmp_path), confirm=False)

        assert toolkit._notifier is default_file_edit_notifier

    def test_edit_gates_all_three_of_its_functions_when_confirming(self, tmp_path):
        toolkit = EditSpec().build(_context(tmp_path), confirm=True)

        gated = {n for n, f in toolkit.functions.items() if f.requires_confirmation}
        async_gated = {n for n, f in toolkit.async_functions.items() if f.requires_confirmation}
        assert (gated | async_gated) == {"edit_file", "edit_file_replace_all", "create_file"}

    def test_bash_hands_the_shell_the_contexts_supervisor(self, tmp_path):
        supervisor = ProcessSupervisor()
        context = ToolBuildContext(base_dir=tmp_path, supervisor=supervisor)

        shell = BashSpec().build(context, confirm=False)

        assert shell._supervisor is supervisor

    def test_bash_gates_running_and_stopping_when_confirming(self, tmp_path):
        shell = BashSpec().build(_context(tmp_path), confirm=True)

        everything = {**shell.functions, **shell.async_functions}
        gated = {n for n, f in everything.items() if f.requires_confirmation}
        assert gated == {"run_shell_command", "stop_process"}

    def test_web_fetch_hands_over_the_broadcast_when_there_is_one(self, tmp_path):
        def sink(event):
            return None

        toolkit = WebFetchSpec().build(_context(tmp_path, broadcast=sink), confirm=False)

        assert toolkit._fetcher._broadcast is sink

    def test_web_fetch_without_a_broadcast_builds_with_none(self, tmp_path):
        toolkit = WebFetchSpec().build(_context(tmp_path), confirm=False)

        assert toolkit._fetcher._broadcast is None

    def test_schedule_reads_the_project_dir_and_never_gates(self, tmp_path):
        toolkit = ScheduleSpec().build(_context(tmp_path), confirm=True)

        assert toolkit._project_dir == str(tmp_path)
        everything = {**toolkit.functions, **toolkit.async_functions}
        assert not any(f.requires_confirmation for f in everything.values())
        assert ScheduleSpec().confirm_function_names == ()

    def test_visualize_registers_no_function_and_gates_nothing(self, tmp_path):
        spec = VisualizeSpec()

        spec.build(_context(tmp_path), confirm=True)

        assert spec.agno_function_names == ()
        assert spec.confirm_function_names == ()


class TestWebSearch:
    def test_the_gate_names_the_functions_the_search_toolkit_registers(self):
        spec = WebSearchSpec()

        assert spec.confirm_function_names == ("web_search", "search_news")
        assert spec.agno_function_names == ("web_search", "search_news")

    def test_it_builds_the_search_toolkit_on_the_auto_backend_and_gated_when_confirming(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(tool_spec, "DuckDuckGoTools", _Probe)

        gated = WebSearchSpec().build(_context(tmp_path), confirm=True)
        open_ = WebSearchSpec().build(_context(tmp_path), confirm=False)

        assert gated.kwargs == {
            "backend": "auto",
            "requires_confirmation_tools": ["web_search", "search_news"],
        }
        assert open_.kwargs == {"backend": "auto"}

    def test_without_the_extra_it_says_how_to_install_it(self, monkeypatch, tmp_path):
        monkeypatch.setattr(tool_spec, "DuckDuckGoTools", None)

        with pytest.raises(ImportError) as caught:
            WebSearchSpec().build(_context(tmp_path), confirm=False)

        assert str(caught.value) == (
            "Web search requires duckduckgo-search. Install: pip install igni-core[web]"
        )

    def test_its_placeholder_toolkit_is_not_a_toolkit_anyone_could_mistake_for_real(self):
        from agno.tools.file import FileTools

        assert WebSearchSpec().toolkit_cls is _UnbuiltToolkit
        assert WebSearchSpec().toolkit_cls is not FileTools

    def test_the_placeholder_refuses_to_be_built(self):
        with pytest.raises(RuntimeError, match="_UnbuiltToolkit is a placeholder"):
            _UnbuiltToolkit()
        with pytest.raises(RuntimeError, match="overrides build"):
            _UnbuiltToolkit(base_dir="x", anything=1)
