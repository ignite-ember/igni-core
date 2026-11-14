"""What the MCP config loader makes of each ``.mcp.json`` it reads.

The loader merges four files in a fixed order (home, group directory,
``<project>/.mcp.json``, ``<project>/.igni/.mcp.json``; later wins by name),
maps each entry onto :class:`MCPServerConfig`, and must treat a file it
cannot use as absent: a repository's ``.mcp.json`` is content anyone can
commit, and a broken one must not stop igni from starting.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from igni_core.core.mcp import config as mcp_config
from igni_core.core.mcp.config import (
    MCPConfigLoader,
    MCPPolicy,
    MCPServerConfig,
    MCPTransport,
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / ".igni").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def _write(path: Path, servers: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return path


def test_a_server_has_the_defaults_an_extension_relies_on() -> None:
    cfg = MCPServerConfig(name="x")

    assert cfg.type is MCPTransport.stdio
    assert cfg.command == ""
    assert cfg.args == []
    assert cfg.env == {}
    assert cfg.url == ""
    assert cfg.cwd == ""
    assert cfg.source_path == ""
    assert cfg.source == "user"
    assert [t.value for t in MCPTransport] == ["stdio", "sse"]


def test_an_entry_is_mapped_field_by_field_with_its_file_and_tier(
    home: Path, tmp_path: Path
) -> None:
    project = tmp_path / "proj"
    path = _write(
        project / ".mcp.json",
        {
            "files": {"command": "fs", "args": ["-r", "/"], "env": {"K": "v"}},
            "remote": {"type": "sse", "url": "https://x.example/sse"},
        },
    )

    servers = MCPConfigLoader(project_dir=project).load()

    files = servers["files"]
    assert (files.name, files.type, files.command) == ("files", MCPTransport.stdio, "fs")
    assert files.args == ["-r", "/"]
    assert files.env == {"K": "v"}
    assert files.source_path == str(path)
    assert files.source == "user"
    remote = servers["remote"]
    assert remote.type is MCPTransport.sse
    assert remote.url == "https://x.example/sse"
    assert remote.command == ""


def test_the_file_never_sets_a_working_directory(home: Path, tmp_path: Path) -> None:
    _write(tmp_path / "proj" / ".mcp.json", {"s": {"command": "c", "cwd": "/somewhere"}})

    assert MCPConfigLoader(project_dir=tmp_path / "proj").load()["s"].cwd == ""


def test_the_order_is_home_then_group_then_project_then_project_igni(
    home: Path, tmp_path: Path
) -> None:
    project = tmp_path / "proj"
    group = tmp_path / "group"
    _write(
        home / ".igni" / ".mcp.json",
        {
            "a": {"command": "home"},
            "b": {"command": "home"},
            "c": {"command": "home"},
            "d": {"command": "home"},
        },
    )
    _write(
        group / "g.json",
        {"b": {"command": "group"}, "c": {"command": "group"}, "d": {"command": "group"}},
    )
    _write(project / ".mcp.json", {"c": {"command": "project"}, "d": {"command": "project"}})
    _write(project / ".igni" / ".mcp.json", {"d": {"command": "project-igni"}})

    servers = MCPConfigLoader(project_dir=project, group_mcps_dir=group).load()

    assert {n: s.command for n, s in servers.items()} == {
        "a": "home",
        "b": "group",
        "c": "project",
        "d": "project-igni",
    }
    assert servers["a"].source_path == str(home / ".igni" / ".mcp.json")
    assert servers["b"].source == "group-policy"
    assert servers["c"].source_path == str(project / ".mcp.json")
    assert servers["d"].source_path == str(project / ".igni" / ".mcp.json")


def test_group_files_are_read_in_name_order_and_only_when_they_end_in_json(
    home: Path, tmp_path: Path
) -> None:
    group = tmp_path / "group"
    _write(group / "b.json", {"s": {"command": "b"}})
    _write(group / "a.json", {"s": {"command": "a"}, "only-a": {"command": "a"}})
    (group / "c.txt").write_text(json.dumps({"mcpServers": {"txt": {"command": "t"}}}))

    servers = MCPConfigLoader(project_dir=tmp_path / "proj", group_mcps_dir=group).load()

    assert servers["s"].command == "b"
    assert "only-a" in servers
    assert "txt" not in servers


def test_a_group_dir_that_does_not_exist_or_is_a_file_is_ignored(
    home: Path, tmp_path: Path
) -> None:
    _write(tmp_path / "proj" / ".mcp.json", {"s": {"command": "p"}})
    afile = tmp_path / "afile"
    afile.write_text("x")

    for group in (tmp_path / "missing", afile):
        servers = MCPConfigLoader(project_dir=tmp_path / "proj", group_mcps_dir=group).load()
        assert list(servers) == ["s"]


@pytest.mark.parametrize(
    "content",
    [
        b"{not json",
        b"",
        b'[{"mcpServers": {}}]',
        b'"text"',
        b"null",
        b'{"mcpServers": []}',
        b'{"mcpServers": "x"}',
        b'{"mcpServers": null}',
        b'{"mcpServers": {"s": {"command": "\xff"}}}',
    ],
)
def test_a_project_file_that_cannot_be_used_adds_nothing_and_does_not_raise(
    home: Path, tmp_path: Path, content: bytes
) -> None:
    project = tmp_path / "proj"
    _write(home / ".igni" / ".mcp.json", {"keep": {"command": "home"}})
    project.mkdir()
    (project / ".mcp.json").write_bytes(content)

    servers = MCPConfigLoader(project_dir=project).load()

    assert list(servers) == ["keep"]


def test_an_invalid_entry_is_skipped_with_a_warning_and_its_siblings_load(
    home: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    project = tmp_path / "proj"
    _write(
        project / ".mcp.json",
        {
            "bad-type": {"type": "carrier-pigeon", "command": "x"},
            "bad-args": {"command": "x", "args": "not-a-list"},
            "bad-env": {"command": "x", "env": ["A=1"]},
            "not-an-object": "just a string",
            "good": {"command": "ok"},
        },
    )

    with caplog.at_level(logging.WARNING, logger="igni_core.core.mcp.config"):
        servers = MCPConfigLoader(project_dir=project).load()

    assert list(servers) == ["good"]
    for name in ("bad-type", "bad-args", "bad-env", "not-an-object"):
        assert f"MCP server '{name}'" in caplog.text
    assert "has invalid config" in caplog.text


def test_a_missing_project_is_not_an_error(home: Path, tmp_path: Path) -> None:
    assert MCPConfigLoader(project_dir=tmp_path / "nowhere").load() == {}


def test_the_project_defaults_to_the_working_directory(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path / ".mcp.json", {"s": {"command": "cwd"}})
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)

    assert MCPConfigLoader().load()["s"].command == "cwd"


class TestPluginServers:
    def test_a_malformed_file_warns_and_leaves_the_servers_alone(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        plugin = tmp_path / "plug"
        plugin.mkdir()
        (plugin / ".mcp.json").write_text("{nope")
        existing = {"k": MCPServerConfig(name="k")}

        with caplog.at_level(logging.WARNING, logger="igni_core.core.mcp.config"):
            MCPConfigLoader(project_dir=tmp_path).load_plugin_servers(plugin, "p", existing)

        assert list(existing) == ["k"]
        assert "Failed to parse plugin MCP config" in caplog.text

    @pytest.mark.parametrize("content", [b"[]", b"null", b'{"mcpServers": []}', b"\xff\xfe"])
    def test_a_file_of_the_wrong_shape_adds_nothing_and_does_not_raise(
        self, tmp_path: Path, content: bytes
    ) -> None:
        plugin = tmp_path / "plug"
        plugin.mkdir()
        (plugin / ".mcp.json").write_bytes(content)
        servers: dict[str, MCPServerConfig] = {}

        MCPConfigLoader(project_dir=tmp_path).load_plugin_servers(plugin, "p", servers)

        assert servers == {}

    def test_servers_are_prefixed_sourced_and_an_invalid_one_is_skipped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        plugin = tmp_path / "plug"
        path = _write(
            plugin / ".mcp.json",
            {
                "srv": {"command": "c", "args": ["1"], "env": {"A": "b"}, "url": "u"},
                "bad": {"type": "nope"},
            },
        )
        servers: dict[str, MCPServerConfig] = {}

        with caplog.at_level(logging.WARNING, logger="igni_core.core.mcp.config"):
            MCPConfigLoader(project_dir=tmp_path).load_plugin_servers(plugin, "acme", servers)

        assert list(servers) == ["acme:srv"]
        cfg = servers["acme:srv"]
        assert (cfg.name, cfg.command, cfg.args, cfg.env, cfg.url) == (
            "acme:srv",
            "c",
            ["1"],
            {"A": "b"},
            "u",
        )
        assert cfg.source == "plugin"
        assert cfg.source_path == str(path)
        assert "Plugin 'acme' MCP server 'bad' has invalid config" in caplog.text

    def test_a_plugin_without_either_file_adds_nothing(self, tmp_path: Path) -> None:
        servers: dict[str, MCPServerConfig] = {}

        MCPConfigLoader(project_dir=tmp_path).load_plugin_servers(tmp_path, "p", servers)

        assert servers == {}

    def test_the_dotted_file_wins_over_the_plain_one(self, tmp_path: Path) -> None:
        plugin = tmp_path / "plug"
        _write(plugin / ".mcp.json", {"s": {"command": "dotted"}})
        _write(plugin / "mcp.json", {"s": {"command": "plain"}})
        servers: dict[str, MCPServerConfig] = {}

        MCPConfigLoader(project_dir=tmp_path).load_plugin_servers(plugin, "p", servers)

        assert servers["p:s"].command == "dotted"

    def test_the_first_plugin_keeps_a_name_both_claim(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        plugin = tmp_path / "plug"
        _write(plugin / ".mcp.json", {"s": {"command": "second"}})
        servers = {"p:s": MCPServerConfig(name="p:s", command="first")}

        with caplog.at_level(logging.WARNING, logger="igni_core.core.mcp.config"):
            MCPConfigLoader(project_dir=tmp_path).load_plugin_servers(plugin, "p", servers)

        assert servers["p:s"].command == "first"
        assert "first-wins" in caplog.text


class TestManagedPolicyFile:
    @pytest.fixture
    def managed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        monkeypatch.setattr(mcp_config, "managed_policy_dir", lambda: tmp_path)
        return tmp_path / "managed-settings.json"

    @pytest.mark.parametrize(
        "content",
        [
            b"[]",
            b'"x"',
            b"null",
            b'{"mcp": null}',
            b'{"mcp": []}',
            b'{"mcp": {"denied": "evil"}}',
            b'{"mcp": {"required": 5}}',
            b"\xff\xfe",
        ],
    )
    def test_a_file_it_cannot_use_is_an_empty_policy(self, managed: Path, content: bytes) -> None:
        managed.write_bytes(content)

        assert MCPPolicy.from_managed_settings() == MCPPolicy()

    def test_a_directory_in_place_of_the_file_is_an_empty_policy(self, managed: Path) -> None:
        managed.mkdir()

        assert MCPPolicy.from_managed_settings() == MCPPolicy()
