"""The LSP config loader's tiers, their order, and the reason it gives for
each file or entry it cannot use. Parse failures are data on the result, never
raised, and never stop the other tiers from loading.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from igni_core.core.lsp.loader import LspConfigLoader, load_lsp_config
from igni_core.core.paths import CONFIG_DIR
from igni_core.core.workspace_trust import WorkspaceTrust, WorkspaceTrustStore


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setattr(Path, "home", lambda: path)
    return path


@pytest.fixture
def project(tmp_path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    return path


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))


def _servers(**entries) -> dict:
    return {"lspServers": entries}


@pytest.fixture
def untrusting(tmp_path) -> WorkspaceTrust:
    """A trust service with its own empty store: no folder is trusted."""
    return WorkspaceTrust(store=WorkspaceTrustStore(path=tmp_path / "trust.json"))


class TestWholeFileFailures:
    def test_a_top_level_list_or_scalar_is_reported_not_raised(self, home, project):
        _write(project / ".lsp.json", [1, 2])
        _write(home / CONFIG_DIR / "lsp.json", "5")
        result = LspConfigLoader(project).load()
        assert result.servers == {}
        assert sorted((e.path.split("/")[-1], e.entry_name, e.reason) for e in result.errors) == [
            (".lsp.json", None, "top-level JSON is not an object"),
            ("lsp.json", None, "top-level JSON is not an object"),
        ]

    def test_an_lsp_servers_that_is_not_an_object_is_a_shape_error(self, home, project):
        _write(project / ".lsp.json", {"lspServers": ["pyright"]})
        result = LspConfigLoader(project).load()
        assert result.servers == {}
        (err,) = result.errors
        assert err.entry_name is None
        assert err.path == str(project / ".lsp.json")
        assert err.reason.startswith("invalid config file shape:")

    def test_a_file_that_is_not_utf8_is_a_read_error(self, home, project):
        (project / ".lsp.json").write_bytes(b'{"lspServers": {"a\xff": {}}}')
        result = LspConfigLoader(project).load()
        (err,) = result.errors
        assert err.entry_name is None
        assert err.reason.startswith("read/decode failed:")

    def test_an_empty_file_is_a_read_error(self, home, project):
        (project / ".lsp.json").write_text("")
        (err,) = LspConfigLoader(project).load().errors
        assert err.reason.startswith("read/decode failed:")

    def test_a_directory_named_lsp_json_is_not_a_config(self, home, project):
        (project / ".lsp.json").mkdir()
        result = LspConfigLoader(project).load()
        assert result.servers == {} and result.errors == []

    def test_a_bad_file_does_not_stop_the_other_tiers(self, home, project):
        _write(home / CONFIG_DIR / "lsp.json", "{broken")
        _write(project / ".lsp.json", _servers(ok={"command": "ok"}))
        result = LspConfigLoader(project).load()
        assert list(result.servers) == ["ok"]
        assert len(result.errors) == 1


class TestEntryFailures:
    def test_each_kind_of_bad_entry_gets_its_own_reason(self, home, project):
        _write(
            project / ".lsp.json",
            _servers(
                text="pyright",
                nothing={"args": ["--stdio"]},
                blank={"command": "   "},
                number={"command": 5},
                good={"command": "good"},
            ),
        )
        result = LspConfigLoader(project).load()
        assert list(result.servers) == ["good"]
        reasons = {e.entry_name: e.reason for e in result.errors}
        assert reasons == {
            "text": "entry is not a mapping",
            "nothing": "missing or blank command",
            "blank": "missing or blank command",
            "number": "missing or blank command",
        }
        assert all(e.path == str(project / ".lsp.json") for e in result.errors)

    def test_args_or_languages_that_are_not_lists_are_refused_with_a_reason(self, home, project):
        _write(
            project / ".lsp.json",
            _servers(
                a={"command": "x", "args": 5},
                b={"command": "x", "args": "--stdio"},
                c={"command": "x", "languages": {"python": True}},
                good={"command": "x", "args": ["--stdio"], "languages": ["python"]},
            ),
        )
        result = LspConfigLoader(project).load()
        assert list(result.servers) == ["good"]
        assert {e.entry_name: e.reason for e in result.errors} == {
            "a": "args and languages must be lists",
            "b": "args and languages must be lists",
            "c": "args and languages must be lists",
        }


class TestTiers:
    def test_no_config_anywhere_is_empty_without_errors(self, home, project):
        result = LspConfigLoader(project, plugin_roots=[(project / "nope", "p")]).load()
        assert result.servers == {} and result.errors == []

    def test_the_user_tier_reads_the_igni_home_directory(self, home, project):
        _write(home / CONFIG_DIR / "lsp.json", _servers(mine={"command": "user-cmd"}))
        assert load_lsp_config(project)["mine"].command == "user-cmd"

    def test_later_plugins_override_earlier_ones_and_the_project_only_by_name(
        self, home, project, tmp_path
    ):
        _write(project / ".lsp.json", _servers(shared={"command": "project"}))
        first, second = tmp_path / "first", tmp_path / "second"
        _write(first / ".lsp.json", _servers(shared={"command": "from-first"}))
        _write(second / ".lsp.json", _servers(shared={"command": "from-second"}))
        out = load_lsp_config(project, [(first, "same"), (second, "same")])
        # Same namespace collides: the later plugin wins; the project's own
        # un-namespaced entry is a different name and stays.
        assert out["same:shared"].command == "from-second"
        assert out["shared"].command == "project"

    def test_the_same_server_name_in_two_plugins_stays_two_servers(self, home, project, tmp_path):
        one, two = tmp_path / "one", tmp_path / "two"
        _write(one / ".lsp.json", _servers(py={"command": "one-py"}))
        _write(two / ".lsp.json", _servers(py={"command": "two-py"}))
        out = load_lsp_config(project, [(one, "one"), (two, "two")])
        assert {n: c.command for n, c in out.items()} == {"one:py": "one-py", "two:py": "two-py"}

    def test_a_plugin_without_a_config_file_is_skipped_silently(self, home, project, tmp_path):
        bare = tmp_path / "bare"
        bare.mkdir()
        result = LspConfigLoader(project, [(bare, "bare")]).load()
        assert result.servers == {} and result.errors == []

    def test_plugin_roots_are_copied_at_construction(self, home, project, tmp_path):
        plugin = tmp_path / "plug"
        _write(plugin / ".lsp.json", _servers(a={"command": "a"}))
        roots = [(plugin, "plug")]
        loader = LspConfigLoader(project, roots)
        roots.clear()
        assert list(loader.load().servers) == ["plug:a"]


class TestTheTrustGate:
    def test_an_untrusted_folder_loses_its_own_servers_but_keeps_user_and_plugin_ones(
        self, home, project, tmp_path, untrusting
    ):
        _write(home / CONFIG_DIR / "lsp.json", _servers(user={"command": "u"}))
        _write(project / ".lsp.json", _servers(mine={"command": "evil"}))
        plugin = tmp_path / "plug"
        _write(plugin / ".lsp.json", _servers(p={"command": "p"}))
        out = load_lsp_config(project, [(plugin, "plug")], trust=untrusting)
        assert sorted(out) == ["plug:p", "user"]

    def test_an_untrusted_folder_is_not_even_read_so_it_reports_no_errors(
        self, home, project, untrusting
    ):
        _write(project / ".lsp.json", "{broken")
        result = LspConfigLoader(project, trust=untrusting).load()
        assert result.servers == {} and result.errors == []

    def test_a_trusted_folder_overrides_the_user_tier(self, home, project, untrusting):
        _write(home / CONFIG_DIR / "lsp.json", _servers(shared={"command": "user"}))
        _write(project / ".lsp.json", _servers(shared={"command": "project"}))
        assert load_lsp_config(project, trust=untrusting)["shared"].command == "user"
        untrusting.trust(project)
        assert load_lsp_config(project, trust=untrusting)["shared"].command == "project"
