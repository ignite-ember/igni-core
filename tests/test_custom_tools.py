"""Custom tool discovery — ``.igni/tools/``, plugins, and the group.

What the loader does is small enough to state in one line: it imports
the ``@tool``-decorated functions a folder brings, under names that
keep a plugin's tools from colliding with the person's own. Everything
else here is about the two things that make that safe or legible:

* **Trust.** A folder's own tools are code — opening a repository must
  not run them. The loader asks a trust service, and the service it
  asks is the one handed to it, not whichever one the process happens
  to share.
* **The result.** The loader returns what it found *and* what it
  skipped or could not load, so a caller can say why a tool the person
  expected is missing.

The tests here pin those two, plus the scan order (which decides which
tool wins a name clash) and the module bookkeeping that lets a caller
drop what was imported.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest
from agno.tools import tool

from igni_core.core.paths import CONFIG_DIR
from igni_core.core.tools.custom_loader import CustomToolkit, CustomToolLoader
from igni_core.core.tools.custom_loader_schemas import PluginToolDir
from igni_core.core.workspace_trust import (
    WorkspaceTrust,
    WorkspaceTrustStore,
    trust_service,
)

# ── helpers ───────────────────────────────────────────────────────


def _write_tool_file(tools_dir: Path, filename: str, content: str) -> Path:
    """Write a tool file into ``tools_dir``, creating the directory."""
    tools_dir.mkdir(parents=True, exist_ok=True)
    path = tools_dir / filename
    path.write_text(textwrap.dedent(content))
    return path


def _one_tool(name: str = "greet") -> str:
    return f"""\
        from agno.tools import tool

        @tool(description="A tool")
        def {name}() -> str:
            return "{name}"
        """


def _marker_tool(marker: Path) -> str:
    """A tool file that writes ``marker`` the moment it is imported."""
    return f"""\
        from pathlib import Path

        Path({str(marker)!r}).write_text("ran")

        from agno.tools import tool

        @tool(description="Writes a marker")
        def marker_tool() -> str:
            return "ran"
        """


class _RecordingTrust(WorkspaceTrust):
    """A trust service that remembers what it was asked."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.asked: list[tuple[Path, str]] = []

    def is_trusted(self, project_dir, *, what: str = "") -> bool:
        self.asked.append((Path(project_dir), what))
        return super().is_trusted(project_dir, what=what)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake ``~``, so the loader's home-directory scan is this test's."""
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake))
    return fake


@pytest.fixture
def trust(tmp_path: Path) -> WorkspaceTrust:
    """A trust service with its own store — no decision of another test's."""
    return WorkspaceTrust(store=WorkspaceTrustStore(path=tmp_path / "trust.json"))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """The folder whose own tools the loader would import."""
    path = tmp_path / "project"
    path.mkdir()
    return path


def _loader(trust: WorkspaceTrust) -> CustomToolLoader:
    return CustomToolLoader(trust=trust)


def _reasons(result) -> dict[Path, str]:
    """The skip reasons, keyed by the path they were recorded for."""
    return {skip.path: skip.reason for skip in result.skipped}


# ── trust ─────────────────────────────────────────────────────────


class TestTheTrustGate:
    def test_an_untrusted_folder_s_tools_are_never_imported(self, home, trust, project, tmp_path):
        """Opening a repository is not consent to run its code."""
        marker = tmp_path / "tool-ran"
        _write_tool_file(project / CONFIG_DIR / "tools", "x.py", _marker_tool(marker))

        result = _loader(trust).discover(project)

        assert not marker.exists()
        assert not result.toolkits

    def test_a_trusted_folder_s_tools_are_imported(self, home, trust, project, tmp_path):
        """Once the person trusts the folder, its tools load."""
        marker = tmp_path / "tool-ran"
        _write_tool_file(project / CONFIG_DIR / "tools", "x.py", _marker_tool(marker))
        trust.trust(project)

        result = _loader(trust).discover(project)

        assert marker.exists()
        assert [tk.name for tk in result.toolkits] == ["custom_x"]

    def test_the_injected_trust_service_is_the_one_consulted(self, home, project, tmp_path):
        """The loader asks the service it was handed.

        The process's service says the folder is trusted and the injected
        one says it is not: the folder's tools stay unloaded, because the
        loader's collaborator is the one that answers.
        """
        marker = tmp_path / "tool-ran"
        _write_tool_file(project / CONFIG_DIR / "tools", "x.py", _marker_tool(marker))

        process_service = WorkspaceTrust(store=WorkspaceTrustStore(path=tmp_path / "process.json"))
        process_service.trust(project)
        trust_service.replace(process_service)
        injected = WorkspaceTrust(store=WorkspaceTrustStore(path=tmp_path / "trust.json"))

        result = _loader(injected).discover(project)

        assert not marker.exists()
        assert not result.toolkits

    def test_the_process_s_trust_service_is_not_consulted(self, home, project, tmp_path):
        """The other way round: the injected service trusts the folder and
        the process's one does not. The folder's tools still load."""
        marker = tmp_path / "tool-ran"
        _write_tool_file(project / CONFIG_DIR / "tools", "x.py", _marker_tool(marker))

        # The process's service trusts nothing, so a loader that reached
        # for it would find the folder untrusted.
        trust_service.replace(
            WorkspaceTrust(store=WorkspaceTrustStore(path=tmp_path / "process.json"))
        )
        injected = WorkspaceTrust(store=WorkspaceTrustStore(path=tmp_path / "trust.json"))
        injected.trust(project)

        result = _loader(injected).discover(project)

        assert marker.exists()
        assert [tk.name for tk in result.toolkits] == ["custom_x"]

    def test_the_loader_asks_about_the_folder_s_own_tools(self, home, project, tmp_path):
        """The question names what is gated, so the log line says why."""
        recorder = _RecordingTrust(store=WorkspaceTrustStore(path=tmp_path / "trust.json"))

        _loader(recorder).discover(project)

        assert recorder.asked == [(project, "tools (.igni/tools)")]

    def test_the_home_directory_is_scanned_without_any_trust(self, home, trust, project):
        """The person's own tools are the person's own — no folder to trust."""
        _write_tool_file(home / CONFIG_DIR / "tools", "mine.py", _one_tool("mine"))

        result = _loader(trust).discover(project)

        assert [tk.name for tk in result.toolkits] == ["custom_mine"]


# ── the order of the sources ──────────────────────────────────────


class TestTheSourceOrder:
    def test_the_sources_are_scanned_home_project_group_plugins(
        self, home, trust, project, tmp_path
    ):
        """Home, then the folder's own, then the group's, then the plugins.

        The order decides which tool wins a name clash, so it is
        pinned rather than assumed.
        """
        _write_tool_file(home / CONFIG_DIR / "tools", "a_home.py", _one_tool("a_home"))
        _write_tool_file(project / CONFIG_DIR / "tools", "b_project.py", _one_tool("b_project"))
        group = tmp_path / "group-tools"
        _write_tool_file(group, "c_group.py", _one_tool("c_group"))
        plugin = tmp_path / "plugin-tools"
        _write_tool_file(plugin, "d_plugin.py", _one_tool("d_plugin"))
        trust.trust(project)

        result = _loader(trust).discover(
            project,
            plugin_tool_dirs=[PluginToolDir(plugin_name="alpha", tools_dir=plugin)],
            group_tools_dir=group,
        )

        assert [tk.name for tk in result.toolkits] == [
            "custom_a_home",
            "custom_b_project",
            "custom_c_group",
            "custom_alpha_d_plugin",
        ]

    def test_the_files_inside_a_source_are_scanned_in_name_order(self, home, trust, project):
        """Within a directory the files are scanned sorted, so two runs
        agree."""
        tools = home / CONFIG_DIR / "tools"
        for stem in ("zebra", "alpha", "middle"):
            _write_tool_file(tools, f"{stem}.py", _one_tool(stem))

        result = _loader(trust).discover(project)

        assert [tk.name for tk in result.toolkits] == [
            "custom_alpha",
            "custom_middle",
            "custom_zebra",
        ]


# ── the plugin namespace ──────────────────────────────────────────


class TestThePluginNamespace:
    def test_a_plugin_s_tools_are_namespaced_by_its_name(self, home, trust, project, tmp_path):
        """A plugin's file lands under ``custom_<plugin>_<file>``."""
        plugin = tmp_path / "plugin-tools"
        _write_tool_file(plugin, "demo.py", _one_tool("demo"))

        result = _loader(trust).discover(
            project,
            plugin_tool_dirs=[PluginToolDir(plugin_name="alpha", tools_dir=plugin)],
        )

        assert [tk.name for tk in result.toolkits] == ["custom_alpha_demo"]

    def test_a_plugin_s_tools_do_not_shadow_the_person_s_own(self, home, trust, project, tmp_path):
        """Same file name on both sides: two toolkits, two names."""
        _write_tool_file(home / CONFIG_DIR / "tools", "demo.py", _one_tool("mine"))
        plugin = tmp_path / "plugin-tools"
        _write_tool_file(plugin, "demo.py", _one_tool("theirs"))

        result = _loader(trust).discover(
            project,
            plugin_tool_dirs=[PluginToolDir(plugin_name="alpha", tools_dir=plugin)],
        )

        names = sorted(tk.name for tk in result.toolkits)
        assert names == ["custom_alpha_demo", "custom_demo"]
        functions = {name for tk in result.toolkits for name in tk.functions}
        assert functions == {"mine", "theirs"}

    def test_two_plugins_with_the_same_file_name_stay_apart(self, home, trust, project, tmp_path):
        """Two plugins, same file name: the plugin name keeps them apart."""
        first = tmp_path / "first"
        second = tmp_path / "second"
        _write_tool_file(first, "demo.py", _one_tool("first"))
        _write_tool_file(second, "demo.py", _one_tool("second"))

        result = _loader(trust).discover(
            project,
            plugin_tool_dirs=[
                PluginToolDir(plugin_name="alpha", tools_dir=first),
                PluginToolDir(plugin_name="beta", tools_dir=second),
            ],
        )

        assert sorted(tk.name for tk in result.toolkits) == [
            "custom_alpha_demo",
            "custom_beta_demo",
        ]


# ── what was skipped ──────────────────────────────────────────────


class TestTheSkippedFiles:
    def test_a_file_starting_with_an_underscore_is_skipped(self, home, trust, project):
        """``_``-prefixed files are the folder's own internals."""
        _write_tool_file(home / CONFIG_DIR / "tools", "_internal.py", _one_tool("hidden"))

        result = _loader(trust).discover(project)

        assert not result.toolkits
        assert _reasons(result)[home / CONFIG_DIR / "tools" / "_internal.py"] == (
            "underscore_prefix"
        )

    def test_a_file_with_no_tools_is_skipped(self, home, trust, project):
        """A file that imports cleanly but contributes no tool is skipped."""
        _write_tool_file(
            home / CONFIG_DIR / "tools",
            "utils.py",
            """\
            def helper():
                return 42
            """,
        )

        result = _loader(trust).discover(project)

        assert not result.toolkits
        assert _reasons(result)[home / CONFIG_DIR / "tools" / "utils.py"] == "no_functions"

    def test_a_missing_directory_is_skipped(self, home, trust, project):
        """A directory that isn't there is reported as such — that is not
        the same as a directory with nothing in it."""
        result = _loader(trust).discover(project)

        assert not result.toolkits
        assert _reasons(result)[home / CONFIG_DIR / "tools"] == "not_a_directory"

    def test_a_directory_with_nothing_in_it_is_not_a_missing_directory(self, home, trust, project):
        """An empty directory is empty — not missing."""
        (home / CONFIG_DIR / "tools").mkdir(parents=True)

        result = _loader(trust).discover(project)

        assert not result.toolkits
        assert not result.skipped

    def test_the_skip_reasons_are_the_three_documented_ones(self, home, trust, project):
        """The reason is a closed set, so a caller can switch on it."""
        tools = home / CONFIG_DIR / "tools"
        _write_tool_file(tools, "_hidden.py", _one_tool("hidden"))
        _write_tool_file(tools, "empty.py", "X = 1\n")

        result = _loader(trust).discover(project)

        assert {skip.reason for skip in result.skipped} == {
            "underscore_prefix",
            "no_functions",
        }
        assert {skip.reason for skip in result.skipped} <= {
            "underscore_prefix",
            "no_functions",
            "not_a_directory",
        }


# ── what failed ───────────────────────────────────────────────────


class TestTheFailedFiles:
    def test_a_file_with_a_syntax_error_is_reported(self, home, trust, project):
        """The error is on the result, not only in the log."""
        _write_tool_file(
            home / CONFIG_DIR / "tools",
            "broken.py",
            """\
            def this is not valid python
            """,
        )

        result = _loader(trust).discover(project)

        assert not result.toolkits
        assert len(result.failed) == 1
        failure = result.failed[0]
        assert failure.path == home / CONFIG_DIR / "tools" / "broken.py"
        assert failure.error_type == "SyntaxError"
        assert failure.error

    def test_a_file_that_cannot_be_imported_is_reported(self, home, trust, project):
        """A missing dependency is reported like any other import error."""
        _write_tool_file(
            home / CONFIG_DIR / "tools",
            "bad_import.py",
            """\
            import nonexistent_module_xyz_12345
            """,
        )

        result = _loader(trust).discover(project)

        assert len(result.failed) == 1
        assert result.failed[0].error_type == "ModuleNotFoundError"
        assert "nonexistent_module_xyz_12345" in result.failed[0].error

    def test_a_failed_file_leaves_no_module_behind(self, home, trust, project):
        """The half-imported module is removed again."""
        _write_tool_file(
            home / CONFIG_DIR / "tools",
            "bad_import.py",
            """\
            import nonexistent_module_xyz_12345
            """,
        )
        loader = _loader(trust)

        loader.discover(project)

        assert "igni_custom_tools.bad_import" not in sys.modules

    def test_one_broken_file_does_not_hide_the_others(self, home, trust, project):
        """A file that fails to import does not stop the scan."""
        _write_tool_file(
            home / CONFIG_DIR / "tools",
            "broken.py",
            """\
            def this is not valid python
            """,
        )
        _write_tool_file(home / CONFIG_DIR / "tools", "good.py", _one_tool("good_tool"))

        result = _loader(trust).discover(project)

        assert [tk.name for tk in result.toolkits] == ["custom_good"]
        assert len(result.failed) == 1


# ── what was loaded ───────────────────────────────────────────────


class TestTheLoadedFiles:
    def test_a_loaded_file_carries_its_toolkit_name_and_function_count(self, home, trust, project):
        """The count is what the file contributed, not what it defines."""
        _write_tool_file(
            home / CONFIG_DIR / "tools",
            "docker.py",
            """\
            from agno.tools import tool

            @tool(description="Up")
            def docker_up() -> str:
                return "up"

            @tool(description="Down")
            def docker_down() -> str:
                return "down"

            def helper():
                return "not a tool"
            """,
        )

        result = _loader(trust).discover(project)

        assert len(result.loaded) == 1
        loaded = result.loaded[0]
        assert loaded.path == home / CONFIG_DIR / "tools" / "docker.py"
        assert loaded.toolkit_name == "custom_docker"
        assert loaded.function_count == 2

    def test_the_toolkit_holds_the_functions_the_file_contributed(self, home, trust, project):
        _write_tool_file(
            home / CONFIG_DIR / "tools",
            "docker.py",
            """\
            from agno.tools import tool

            @tool(description="Up")
            def docker_up() -> str:
                return "up"

            @tool(description="Down")
            def docker_down() -> str:
                return "down"

            def helper():
                return "not a tool"
            """,
        )

        result = _loader(trust).discover(project)

        toolkit = result.toolkits[0]
        assert set(toolkit.functions) == {"docker_up", "docker_down"}

    def test_every_file_looked_at_is_accounted_for(self, home, trust, project):
        """Each file is loaded, skipped or failed — never simply lost."""
        tools = home / CONFIG_DIR / "tools"
        _write_tool_file(tools, "good.py", _one_tool("good"))
        _write_tool_file(tools, "_hidden.py", _one_tool("hidden"))
        _write_tool_file(tools, "empty.py", "X = 1\n")
        _write_tool_file(
            tools,
            "broken.py",
            """\
            def this is not valid python
            """,
        )

        result = _loader(trust).discover(project)

        assert len(result.loaded) == 1
        assert len(result.skipped) == 2
        assert len(result.failed) == 1


# ── the module bookkeeping ────────────────────────────────────────


class TestUnload:
    def test_unload_removes_the_modules_the_loader_installed(self, home, trust, project):
        """The loader knows what it put on ``sys.modules`` and can take it
        back down."""
        _write_tool_file(home / CONFIG_DIR / "tools", "greet.py", _one_tool("greet"))
        loader = _loader(trust)

        loader.discover(project)
        assert "igni_custom_tools.greet" in sys.modules

        loader.unload()
        assert "igni_custom_tools.greet" not in sys.modules

    def test_unload_leaves_the_modules_it_did_not_install_alone(self, home, trust, project):
        """``unload`` drops what this loader installed, not whatever else
        happens to be on ``sys.modules``."""
        _write_tool_file(home / CONFIG_DIR / "tools", "greet.py", _one_tool("greet"))
        loader = _loader(trust)
        sentinel = "igni_custom_tools.not_ours"
        sys.modules[sentinel] = ModuleType("igni_custom_tools.not_ours")
        try:
            loader.discover(project)

            loader.unload()

            assert sentinel in sys.modules
        finally:
            sys.modules.pop(sentinel, None)

    def test_unload_twice_is_safe(self, home, trust, project):
        """Nothing was installed, or everything already went: dropping the
        modules again is a no-op, not an error."""
        _write_tool_file(home / CONFIG_DIR / "tools", "greet.py", _one_tool("greet"))
        loader = _loader(trust)
        loader.discover(project)

        loader.unload()
        loader.unload()

        assert not loader._loaded_module_names
        assert "igni_custom_tools.greet" not in sys.modules

    def test_unload_is_safe_when_nothing_was_loaded(self, home, trust, project):
        """Nothing loaded, nothing to drop."""
        loader = _loader(trust)

        loader.unload()

        assert not loader._loaded_module_names


# ── the toolkit itself ────────────────────────────────────────────


class TestCustomToolkit:
    def test_toolkit_name(self):
        @tool(description="Test")
        def my_fn() -> str:
            return "hi"

        tk = CustomToolkit(name="custom_test", functions=[my_fn])
        assert tk.name == "custom_test"
        assert "my_fn" in tk.functions

    def test_toolkit_functions_callable(self):
        @tool(description="Add two numbers")
        def add(a: int, b: int) -> int:
            return a + b

        tk = CustomToolkit(name="custom_math", functions=[add])
        func = tk.functions["add"]
        assert func is not None


# ── the edges of a scan ───────────────────────────────────────────


class TestTheEdgesOfAScan:
    def test_a_tool_file_that_exits_at_import_is_a_failed_file_not_the_end_of_igni(
        self, home, trust, project
    ):
        tools_dir = home / CONFIG_DIR / "tools"
        _write_tool_file(tools_dir, "a_quits.py", "import sys\nsys.exit(3)\n")
        _write_tool_file(tools_dir, "b_fine.py", _one_tool("fine"))
        loader = _loader(trust)

        result = loader.discover(project)

        [failed] = result.failed
        assert (failed.path.name, failed.error_type, failed.error) == (
            "a_quits.py",
            "SystemExit",
            "3",
        )
        assert [tk.name for tk in result.toolkits] == ["custom_b_fine"]
        assert "igni_custom_tools.a_quits" not in sys.modules
        assert loader._loaded_module_names == ["igni_custom_tools.b_fine"]

    def test_a_path_importlib_cannot_make_a_spec_for_is_a_failed_file(
        self, home, trust, project, monkeypatch
    ):
        _write_tool_file(home / CONFIG_DIR / "tools", "odd.py", _one_tool("odd"))
        monkeypatch.setattr(
            "igni_core.core.tools.custom_loader.importlib.util.spec_from_file_location",
            lambda name, path: None,
        )
        loader = _loader(trust)

        result = loader.discover(project)

        [failed] = result.failed
        assert failed.path.name == "odd.py"
        assert failed.error == "importlib returned no spec/loader for this path"
        assert failed.error_type == "ImportError"
        assert not result.toolkits
        assert loader._loaded_module_names == []

    def test_with_no_project_dir_the_working_directory_is_the_project(
        self, home, trust, project, monkeypatch
    ):
        _write_tool_file(project / CONFIG_DIR / "tools", "here.py", _one_tool("here"))
        trust.trust(project)
        monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: project))

        result = _loader(trust).discover()

        assert [tk.name for tk in result.toolkits] == ["custom_here"]

    def test_with_no_project_dir_an_untrusted_working_directory_loads_nothing(
        self, home, trust, project, monkeypatch
    ):
        _write_tool_file(project / CONFIG_DIR / "tools", "here.py", _one_tool("here"))
        monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: project))

        assert not _loader(trust).discover().toolkits

    def test_only_python_files_in_the_folder_itself_are_looked_at(self, home, trust, project):
        tools_dir = home / CONFIG_DIR / "tools"
        _write_tool_file(tools_dir, "real.py", _one_tool("real"))
        _write_tool_file(tools_dir, "notes.txt", _one_tool("notes"))
        _write_tool_file(tools_dir, "tool.pyc", "garbage")
        _write_tool_file(tools_dir / "nested", "deep.py", _one_tool("deep"))
        (tools_dir / "dir.py").mkdir()

        result = _loader(trust).discover(project)

        assert [tk.name for tk in result.toolkits] == ["custom_real"]
        assert [Path(f.path).name for f in result.loaded] == ["real.py"]
        assert [s.path for s in result.skipped if s.reason != "not_a_directory"] == []

    def test_an_empty_folder_is_a_directory_with_nothing_in_it_not_a_missing_one(
        self, home, trust, project
    ):
        (home / CONFIG_DIR / "tools").mkdir(parents=True)

        result = _loader(trust).discover(project)

        assert not any(s.path == home / CONFIG_DIR / "tools" for s in result.skipped)
        assert not result.toolkits

    def test_a_file_name_with_unicode_loads_under_that_name(self, home, trust, project):
        _write_tool_file(home / CONFIG_DIR / "tools", "größe.py", _one_tool("size"))

        result = _loader(trust).discover(project)

        assert [tk.name for tk in result.toolkits] == ["custom_größe"]

    def test_functions_imported_from_another_module_count_as_the_file_s_tools(
        self, home, trust, project
    ):
        tools_dir = home / CONFIG_DIR / "tools"
        _write_tool_file(tools_dir, "helpers_src.py", _one_tool("shared"))
        _write_tool_file(
            tools_dir, "z_user.py", "from igni_custom_tools.helpers_src import shared\n"
        )

        result = _loader(trust).discover(project)

        by_name = {tk.name: sorted(tk.functions) for tk in result.toolkits}
        assert by_name == {"custom_helpers_src": ["shared"], "custom_z_user": ["shared"]}

    def test_a_second_discover_adds_to_what_unload_will_drop(self, home, trust, project):
        _write_tool_file(home / CONFIG_DIR / "tools", "greet.py", _one_tool("greet"))
        loader = _loader(trust)

        loader.discover(project)
        loader.discover(project)

        assert loader._loaded_module_names == ["igni_custom_tools.greet"] * 2
        loader.unload()
        assert "igni_custom_tools.greet" not in sys.modules

    def test_the_same_file_name_in_two_sources_gives_two_toolkits_and_one_module(
        self, home, trust, project
    ):
        _write_tool_file(home / CONFIG_DIR / "tools", "dup.py", _one_tool("from_home"))
        group = project.parent / "group_tools"
        _write_tool_file(group, "dup.py", _one_tool("from_group"))

        loader = _loader(trust)

        result = loader.discover(project, group_tools_dir=group)

        assert [(tk.name, sorted(tk.functions)) for tk in result.toolkits] == [
            ("custom_dup", ["from_home"]),
            ("custom_dup", ["from_group"]),
        ]
        # Both are imported as ``igni_custom_tools.dup``: the later one is what
        # ``sys.modules`` holds, and unload removes it once however often it was named.
        assert hasattr(sys.modules["igni_custom_tools.dup"], "from_group")
        loader.unload()
        assert "igni_custom_tools.dup" not in sys.modules


class TestTheLoadedLogLines:
    def test_a_loaded_file_is_logged_with_its_count_and_path(self, home, trust, project, caplog):
        path = _write_tool_file(home / CONFIG_DIR / "tools", "greet.py", _one_tool("greet"))

        with caplog.at_level("INFO", logger="igni_core.core.tools.custom_loader"):
            _loader(trust).discover(project)

        assert [r.getMessage() for r in caplog.records if r.levelname == "INFO"] == [
            f"Loaded 1 custom tool(s) from {path}"
        ]

    def test_a_plugin_file_is_logged_with_the_plugin_s_name(
        self, home, trust, project, tmp_path, caplog
    ):
        path = _write_tool_file(tmp_path / "plug" / "tools", "t.py", _one_tool("t"))
        plugin = PluginToolDir(plugin_name="acme", tools_dir=tmp_path / "plug" / "tools")

        with caplog.at_level("INFO", logger="igni_core.core.tools.custom_loader"):
            _loader(trust).discover(project, plugin_tool_dirs=[plugin])

        assert [r.getMessage() for r in caplog.records if r.levelname == "INFO"] == [
            f"Loaded 1 custom tool(s) from plugin 'acme' file {path}"
        ]

    def test_a_file_that_fails_is_logged_as_a_warning(self, home, trust, project, caplog):
        path = _write_tool_file(home / CONFIG_DIR / "tools", "bad.py", "raise ValueError('nope')\n")

        with caplog.at_level("INFO", logger="igni_core.core.tools.custom_loader"):
            _loader(trust).discover(project)

        assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
            ("WARNING", f"Failed to load custom tool file {path}: nope")
        ]
