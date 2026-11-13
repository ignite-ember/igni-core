"""What the tool registry does with the names it is given and the
collaborators it is built with.

Real registries over the real catalog: the registry is the one place a
name becomes a toolkit, so an unknown name, an alias, a denied tool or a
tool the person registered themselves must each end the way the agents
and the SDKs rely on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.core.tools.process_supervisor import ProcessSupervisor
from igni_core.core.tools.process_supervisor_locator import SupervisorLocator
from igni_core.core.tools.registry import ToolRegistry
from igni_core.core.tools.tool_spec import ToolResolutionRequest


class _Permissions:
    """The two questions the registry asks, answered from sets."""

    def __init__(self, denied=(), ask=()):
        self.denied, self.ask = set(denied), set(ask)

    def is_denied(self, name: str) -> bool:
        return name in self.denied

    def needs_confirmation(self, name: str) -> bool:
        return name in self.ask


def _registry(tmp_path, **kw) -> ToolRegistry:
    kw.setdefault("permissions", _Permissions())
    return ToolRegistry(base_dir=str(tmp_path), **kw)


class TestResolve:
    def test_an_unknown_name_raises_naming_it_and_the_tools_there_are(self, tmp_path):
        registry = _registry(tmp_path)

        with pytest.raises(ValueError) as caught:
            registry.resolve(["Bash", "Nope", "AlsoNope"])

        assert str(caught.value) == f"Unknown tool: 'Nope'. Available: {registry.available_tools}"

    def test_a_comma_separated_string_and_a_list_resolve_alike(self, tmp_path):
        registry = _registry(tmp_path)

        from_text = registry.resolve("Bash, Write")
        from_list = registry.resolve(["Bash", "Write"])

        assert [type(t) for t in from_text] == [type(t) for t in from_list]
        assert len(from_text) == 2

    def test_nothing_asked_resolves_nothing(self, tmp_path):
        registry = _registry(tmp_path)

        assert registry.resolve([]) == []
        assert registry.resolve("") == []

    def test_an_alias_and_its_canonical_name_build_one_toolkit(self, tmp_path):
        assert len(_registry(tmp_path).resolve(["Bash", "BashOutput", "Bash"])) == 1

    def test_mcp_and_reserved_names_are_passed_over_without_complaint(self, tmp_path):
        result = _registry(tmp_path).resolve_typed(
            ToolResolutionRequest(tool_names=["MCP:github", "Orchestrate"])
        )

        assert result.tools == []
        assert result.unknown == []
        assert result.skipped_denied == []

    def test_a_denied_tool_is_skipped_and_reported_not_raised(self, tmp_path):
        registry = _registry(tmp_path, permissions=_Permissions(denied={"Write"}))

        result = registry.resolve_typed(ToolResolutionRequest(tool_names=["Write", "Bash"]))

        assert result.skipped_denied == ["Write"]
        assert len(result.tools) == 1
        assert registry.resolve(["Write"]) == []

    def test_a_tool_that_needs_asking_is_built_to_confirm(self, tmp_path):
        registry = _registry(tmp_path, permissions=_Permissions(ask={"Bash"}))

        (shell,) = registry.resolve(["Bash"])
        (free,) = _registry(tmp_path).resolve(["Bash"])

        assert "run_shell_command" in shell.requires_confirmation_tools
        assert not free.requires_confirmation_tools


class TestRegisteredFactories:
    def test_a_registered_name_is_listed_sorted_with_the_built_ins(self, tmp_path):
        registry = _registry(tmp_path)
        before = registry.available_tools
        registry.register("Aardvark", lambda confirm: object())

        after = registry.available_tools

        assert after == sorted(before + ["Aardvark"])
        assert "BashOutput" in after, "aliases are listed beside the canonical names"
        assert "Aardvark" not in _registry(tmp_path).available_tools, "per instance"

    def test_a_registered_factory_overrides_the_built_in_of_the_same_name(self, tmp_path):
        registry = _registry(tmp_path)
        made = []
        registry.register("Write", lambda confirm: made.append(confirm) or "mine")

        assert registry.resolve(["Write", "Write"]) == ["mine"]
        assert made == [False]

    def test_registering_again_replaces_the_earlier_factory(self, tmp_path):
        registry = _registry(tmp_path)
        registry.register("T", lambda confirm: "one")
        registry.register("T", lambda confirm: "two")

        assert registry.resolve(["T"]) == ["two"]

    def test_denial_wins_over_a_registered_factory(self, tmp_path):
        registry = _registry(tmp_path, permissions=_Permissions(denied={"T"}))
        registry.register("T", lambda confirm: "mine")

        assert registry.resolve(["T"]) == []

    def test_the_factory_is_told_whether_to_confirm(self, tmp_path):
        registry = _registry(tmp_path, permissions=_Permissions(ask={"T"}))
        registry.register("T", lambda confirm: confirm)

        assert registry.resolve(["T"]) == [True]


class TestNormalizeAgnoNames:
    def test_function_names_map_to_registry_names_without_repeats(self):
        names = ToolRegistry.normalize_agno_names(["run_shell_command", "save_file", "Bash"])

        assert names == ["Bash", "Write"]

    def test_an_mcp_name_passes_through(self):
        assert ToolRegistry.normalize_agno_names(["MCP:x"]) == ["MCP:x"]

    def test_an_unknown_name_raises_with_the_catalogs_message(self):
        with pytest.raises(ValueError, match=r"^Unknown tool\(s\): nope, zip\. Available: Bash, "):
            ToolRegistry.normalize_agno_names(["nope", "Bash", "zip"])


class TestWhatItIsBuiltWith:
    def test_it_defaults_to_the_working_directory_and_no_cloud(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)

        registry = ToolRegistry(permissions=_Permissions())  # type: ignore[arg-type]

        assert registry.base_dir == Path.cwd()
        assert registry.cloud_connected is False
        assert registry.file_edit_notifier is None

    @pytest.mark.parametrize(("token", "connected"), [(None, False), ("t", True), ("", True)])
    def test_the_cloud_is_connected_when_a_token_was_given(self, tmp_path, token, connected):
        assert _registry(tmp_path, cloud_token=token).cloud_connected is connected

    def test_a_registry_on_its_own_gets_its_own_supervisor(self, tmp_path):
        first, second = _registry(tmp_path), _registry(tmp_path)

        assert isinstance(first.supervisor, ProcessSupervisor)
        assert first.supervisor is not second.supervisor

    def test_the_backends_supervisor_reaches_the_shell_toolkit(self, tmp_path):
        shared = SupervisorLocator().supervisor
        registry = _registry(tmp_path, supervisor=shared)

        (shell,) = registry.resolve(["Bash"])

        assert registry.supervisor is shared
        assert shell._supervisor is shared


class _Trust:
    def __init__(self, trusted: bool):
        self.trusted = trusted
        self.asked: list[Path] = []

    def is_trusted(self, folder, *, what: str) -> bool:
        self.asked.append(Path(folder))
        return self.trusted


class TestCustomTools:
    @staticmethod
    def _project_with_a_tool(root: Path) -> Path:
        tools = root / ".igni" / "tools"
        tools.mkdir(parents=True)
        (tools / "hello.py").write_text(
            "from agno.tools import tool\n\n"
            "@tool\n"
            "def hello() -> str:\n"
            '    """Say hello."""\n'
            '    return "hi"\n'
        )
        return root

    def test_the_injected_trust_decides_whether_the_projects_tools_load(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        project = self._project_with_a_tool(tmp_path / "proj")

        trusting = _Trust(True)
        loaded = _registry(tmp_path, trust=trusting).load_custom_tools(project)  # type: ignore[arg-type]
        refusing = _Trust(False)
        refused = _registry(tmp_path, trust=refusing).load_custom_tools(project)  # type: ignore[arg-type]

        assert [t.name for t in loaded.toolkits] == ["custom_hello"]
        assert loaded.loaded[0].function_count == 1
        assert refused.toolkits == []
        assert trusting.asked == [project] and refusing.asked == [project]

    def test_the_project_defaults_to_the_registrys_base_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        project = self._project_with_a_tool(tmp_path / "proj")
        trust = _Trust(True)

        result = ToolRegistry(
            base_dir=str(project),
            permissions=_Permissions(),  # type: ignore[arg-type]
            trust=trust,  # type: ignore[arg-type]
        ).load_custom_tools()

        assert trust.asked == [project]
        assert [t.name for t in result.toolkits] == ["custom_hello"]

    def test_what_was_skipped_is_part_of_the_answer(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        project = self._project_with_a_tool(tmp_path / "proj")
        (project / ".igni" / "tools" / "_private.py").write_text("x = 1\n")

        result = _registry(tmp_path, trust=_Trust(True)).load_custom_tools(project)  # type: ignore[arg-type]

        reasons = {(s.path.name, s.reason) for s in result.skipped}
        assert ("_private.py", "underscore_prefix") in reasons
        assert ("tools", "not_a_directory") in reasons, "the missing home tools dir is reported"
