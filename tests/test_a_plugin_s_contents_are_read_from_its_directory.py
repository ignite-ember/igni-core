"""A plugin's contents panel lists what its directory actually bundles.

``PluginDirectoryScanner.scan`` is what the Plugins panel shows for an
installed plugin and for a marketplace entry being previewed from a
shallow clone. It had no test at all: every one of the six readers
(skills, agents, hooks, MCP servers, tools, README) could have stopped
finding anything and the panel would simply have gone empty.

Each test builds a real plugin directory under ``tmp_path`` and reads it
through the scanner, the way both callers do.
"""

from __future__ import annotations

import json
from pathlib import Path

from igni_core.backend.plugin_directory_scanner import PluginDirectoryScanner


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _full_plugin(root: Path) -> None:
    _write(
        root / "skills" / "deploy" / "SKILL.md",
        '---\nname: deploy\ndescription: "Ship it"\n---\nbody',
    )
    _write(root / "skills" / "audit" / "SKILL.md", "---\nDescription: Check things\n---\n")
    (root / "skills" / "bare").mkdir(parents=True)
    _write(root / "skills" / "loose-file.md", "not a skill directory")
    _write(root / "agents" / "reviewer.md", "---\ndescription: Reviews diffs\n---\nprompt")
    _write(root / "agents" / "plain.md", "no frontmatter at all")
    _write(root / "agents" / "notes.txt", "ignored")
    _write(
        root / "hooks" / "hooks.json",
        json.dumps({"hooks": {"PreToolUse": [{}, {}], "Stop": [{}], "Odd": "not-a-list"}}),
    )
    _write(
        root / ".mcp.json",
        json.dumps(
            {
                "mcpServers": {
                    "local": {"command": "node server.js"},
                    "remote": {"type": "http", "url": "https://mcp.internal/x"},
                    "empty": {},
                }
            }
        ),
    )
    _write(root / "tools" / "lookup.py", "")
    _write(root / "tools" / "_private.py", "")
    _write(root / "tools" / "__init__.py", "")
    _write(root / "README.md", "# Hello\n")


def test_every_kind_of_bundled_content_is_listed(tmp_path):
    _full_plugin(tmp_path)

    contents = PluginDirectoryScanner(tmp_path, name="demo").scan()

    assert contents.name == "demo"
    assert contents.root_path == str(tmp_path)
    assert [(s.name, s.description) for s in contents.skills] == [
        ("audit", "Check things"),
        ("bare", ""),
        ("deploy", "Ship it"),
    ]
    assert [(a.name, a.description) for a in contents.agents] == [
        ("plain", ""),
        ("reviewer", "Reviews diffs"),
    ]
    assert [(h.event, h.count) for h in contents.hooks] == [("PreToolUse", 2), ("Stop", 1)]
    assert [(m.name, m.transport, m.command) for m in contents.mcp_servers] == [
        ("local", "stdio", "node server.js"),
        ("remote", "http", "https://mcp.internal/x"),
        ("empty", "stdio", ""),
    ]
    assert [t.name for t in contents.tools] == ["lookup"]
    assert contents.readme == "# Hello\n"
    assert contents.error == ""


def test_an_empty_directory_has_nothing_to_list(tmp_path):
    contents = PluginDirectoryScanner(tmp_path, name="empty").scan()

    assert (contents.skills, contents.agents, contents.hooks) == ([], [], [])
    assert (contents.mcp_servers, contents.tools, contents.readme) == ([], [], "")


def test_broken_json_hides_that_section_and_nothing_else(tmp_path):
    _write(tmp_path / "hooks" / "hooks.json", "{not json")
    _write(tmp_path / ".mcp.json", "{also not json")
    _write(tmp_path / "mcp.json", json.dumps({"mcpServers": {"fallback": {"command": "x"}}}))
    _write(tmp_path / "tools" / "t.py", "")

    contents = PluginDirectoryScanner(tmp_path, name="broken").scan()

    assert contents.hooks == []
    # The first MCP file found is the one read, even when it cannot be:
    # a broken ``.mcp.json`` does not fall back to ``mcp.json``.
    assert contents.mcp_servers == []
    assert [t.name for t in contents.tools] == ["t"]


def test_mcp_json_without_a_dot_is_read_when_it_is_the_only_one(tmp_path):
    _write(tmp_path / "mcp.json", json.dumps({"mcpServers": {"only": {"command": "run"}}}))
    _write(tmp_path / "hooks" / "hooks.json", json.dumps({}))

    contents = PluginDirectoryScanner(tmp_path, name="p").scan()

    assert [m.name for m in contents.mcp_servers] == ["only"]
    assert contents.hooks == []


def test_a_huge_readme_is_cut_and_says_so(tmp_path):
    _write(tmp_path / "readme.md", "x" * 250_000)

    contents = PluginDirectoryScanner(tmp_path, name="big").scan()

    assert contents.readme.startswith("x" * 200_000)
    assert len(contents.readme) < 200_100
    assert contents.readme.endswith("README truncated — open the source repo for the rest._")


def test_frontmatter_that_never_closes_gives_no_description(tmp_path):
    _write(tmp_path / "agents" / "open.md", "---\ndescription: never closed\n")
    _write(tmp_path / "agents" / "other.md", "---\nname: other\n---\n")

    contents = PluginDirectoryScanner(tmp_path, name="p").scan()

    assert [(a.name, a.description) for a in contents.agents] == [("open", ""), ("other", "")]


def test_json_of_the_wrong_shape_hides_that_section_instead_of_failing(tmp_path):
    _write(tmp_path / "hooks" / "hooks.json", json.dumps(["not", "an", "object"]))
    _write(tmp_path / ".mcp.json", json.dumps({"mcpServers": {"bad": "not-an-object"}}))
    _write(tmp_path / "tools" / "t.py", "")

    contents = PluginDirectoryScanner(tmp_path, name="odd").scan()

    assert (contents.hooks, contents.mcp_servers) == ([], [])
    assert [t.name for t in contents.tools] == ["t"]


def test_a_skipped_file_is_logged_with_the_reason(tmp_path, caplog):
    _write(tmp_path / "hooks" / "hooks.json", "{not json")

    with caplog.at_level("WARNING", logger="igni_core.backend.plugin_directory_scanner"):
        PluginDirectoryScanner(tmp_path, name="broken").scan()

    assert "hooks.json" in caplog.text
    assert "broken" in caplog.text


def test_a_description_with_a_colon_keeps_its_full_text(tmp_path):
    _write(tmp_path / "agents" / "a.md", "---\ndescription: Use when: things break\n---\n")

    contents = PluginDirectoryScanner(tmp_path, name="p").scan()

    assert contents.agents[0].description == "Use when: things break"
