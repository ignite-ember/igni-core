"""``Scaffold`` names, writes and escapes what ``igni ext init`` hands an author.

The CLI path is pinned in ``test_a_new_extension_author_is_not_tripped_by_the_cli``;
this pins the class itself: how an id and the package names derive from a
directory, exactly which manifest each scaffold builds, that the files returned
are the files written, that a title which is only a directory name cannot break
the page or the Python it lands in, and that the dev backend is a real MCP
server.
"""

from __future__ import annotations

import ast
import html
import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from igni_core.cli.ext_scaffold import SCHEMA_URL, Scaffold

# ── names ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "directory,expected",
    [
        ("tickets", "local.tickets"),
        ("My-Tickets", "local.mytickets"),
        ("tickets_v2", "local.ticketsv2"),
        ("a b  c", "local.abc"),
        ("café", "local.caf"),
        ("---", "local."),
    ],
)
def test_the_default_id_is_the_directory_name_without_anything_but_letters_and_digits(
    tmp_path: Path, directory: str, expected: str
) -> None:
    assert Scaffold(tmp_path / directory, None).id == expected


def test_an_explicit_id_is_used_as_given(tmp_path: Path) -> None:
    assert Scaffold(tmp_path / "x", "acme.tickets").id == "acme.tickets"


def test_the_python_names_follow_the_id(tmp_path: Path) -> None:
    scaffold = Scaffold(tmp_path / "x", "acme.tickets")
    assert scaffold.module == "acme_tickets_extension"
    assert scaffold.distribution == "acme-tickets-extension"


def test_the_label_is_the_title_cut_to_24_characters(tmp_path: Path) -> None:
    long = "a" * 24 + "bcdef"
    assert Scaffold(tmp_path / long, "acme.x").label == "a" * 24
    assert Scaffold(tmp_path / "short", "acme.x").label == "short"


# ── the manifests ───────────────────────────────────────────────────


def test_the_dev_manifest_runs_this_interpreter_and_grants_only_its_own_rpc(tmp_path: Path) -> None:
    manifest = Scaffold(tmp_path / "tickets", "acme.tickets").dev_manifest()
    assert manifest.dev is not None and manifest.dev.command == [sys.executable, "server.py"]
    assert manifest.permissions.rpc == ["ext.acme.tickets.*"]
    assert manifest.permissions.tools == []
    assert [(p.kind, p.label, p.sidebar) for p in manifest.contributes.pages] == [
        ("acme.tickets.main", "tickets", True)
    ]
    assert manifest.trust.value == "unsigned"


def test_the_service_manifest_declares_two_read_only_tools_one_rpc_and_a_hook(
    tmp_path: Path,
) -> None:
    manifest = Scaffold(tmp_path / "tickets", "acme.tickets").service_manifest()
    tools = {t.name: t.annotations for t in manifest.permissions.tools}
    assert list(tools) == ["acme.tickets.lookup_ticket", "acme.tickets.ticket_from_service"]
    for name, hints in tools.items():
        assert (hints.read_only, hints.destructive, hints.idempotent) == (True, False, True)
        assert hints.open_world is name.endswith("ticket_from_service")
    assert manifest.permissions.rpc == ["ext.acme.tickets.lookup_ticket"]
    assert [h.value for h in manifest.permissions.hooks] == ["PostToolUse"]
    assert manifest.vm is not None and manifest.vm.memory_mib == 2048
    assert manifest.vm.packages.apt == ["jq"]
    assert manifest.python is not None and manifest.python.package == "acme-tickets-extension"
    assert manifest.trust.value == "partner"


# ── what is written ─────────────────────────────────────────────────


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("write", ["write_dev", "write_service"])
def test_the_files_returned_are_exactly_the_files_written(tmp_path: Path, write: str) -> None:
    target = tmp_path / "deep" / "er" / "tickets"
    returned = getattr(Scaffold(target, "acme.tickets"), write)()
    assert len(returned) == len(set(returned))
    assert set(returned) == _files(target)


def test_the_manifest_is_the_scaffolds_own_fields_with_the_schema_first(tmp_path: Path) -> None:
    Scaffold(tmp_path, "acme.tickets").write_dev()
    raw = (tmp_path / "manifest.json").read_text()
    assert raw.endswith("}\n") and raw.startswith('{\n  "$schema": ')
    fields = json.loads(raw)
    assert list(fields)[0] == "$schema" and fields["$schema"] == SCHEMA_URL
    assert fields["id"] == "acme.tickets"
    assert "description" not in fields  # unset fields are not written


def test_existing_files_in_the_target_are_overwritten(tmp_path: Path) -> None:
    (tmp_path / "server.py").write_text("old")
    Scaffold(tmp_path, "acme.tickets").write_dev()
    assert "MCPServer" in (tmp_path / "server.py").read_text()


def test_an_id_the_contract_refuses_writes_nothing(tmp_path: Path) -> None:
    target = tmp_path / "new"
    for write in ("write_dev", "write_service"):
        with pytest.raises(ValidationError):
            getattr(Scaffold(target, "Acme Tickets"), write)()
    assert not target.exists()


def test_a_directory_with_only_symbols_derives_an_id_the_contract_refuses(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Scaffold(tmp_path / "---", None).write_dev()
    assert not (tmp_path / "---").exists()


# ── filling the templates ───────────────────────────────────────────


def test_a_token_nobody_fills_is_a_key_error(tmp_path: Path) -> None:
    with pytest.raises(KeyError, match="NOPE"):
        Scaffold(tmp_path, "acme.x")._fill("a {{NOPE}} b")


def test_a_part_may_be_filled_and_an_unknown_part_is_not_invented(tmp_path: Path) -> None:
    scaffold = Scaffold(tmp_path, "acme.x")
    assert scaffold._fill("{{ID}}/{{BODY}}", {"BODY": "b"}) == "acme.x/b"


def test_what_was_inserted_is_not_expanded_again(tmp_path: Path) -> None:
    target = tmp_path / "{{ID}}{{NOPE}}"
    scaffold = Scaffold(target, "acme.x")
    assert scaffold._fill("<{{TITLE}}>") == "<{{ID}}{{NOPE}}>"
    scaffold.write_service()
    assert "{{ID}}{{NOPE}}" in (target / "README.md").read_text()


# ── a hostile directory name ────────────────────────────────────────

HOSTILE = 'x"""\\<b>&\'; import os #'


@pytest.mark.parametrize("subdir,write", [("ui", "write_dev"), ("extension/ui", "write_service")])
def test_a_title_with_markup_stays_text_in_the_page(
    tmp_path: Path, subdir: str, write: str
) -> None:
    target = tmp_path / HOSTILE
    getattr(Scaffold(target, "acme.tickets"), write)()
    page = (target / subdir / "index.html").read_text()
    assert "<title>" + html.escape(HOSTILE, quote=False) + "</title>" in page
    assert "<b>" not in page


def test_a_title_with_quotes_and_backslashes_leaves_the_python_valid(tmp_path: Path) -> None:
    target = tmp_path / HOSTILE
    Scaffold(target, "acme.tickets").write_service()
    for source in (target / "extension" / "src").rglob("*.py"):
        tree = ast.parse(source.read_text())
        assert HOSTILE in (ast.get_docstring(tree) or "")
    service = ast.parse((target / "service" / "service.py").read_text())
    assert (ast.get_docstring(service) or "").startswith(HOSTILE + "'s service")


def test_an_ordinary_title_is_written_unchanged(tmp_path: Path) -> None:
    target = tmp_path / "Tickets"
    Scaffold(target, "acme.tickets").write_service()
    assert (target / "extension/ui/index.html").read_text().count("<title>Tickets</title>") == 1
    extension = (target / "extension/src/acme_tickets_extension/__init__.py").read_text()
    assert extension.startswith('"""Tickets: two tools, one hook')


# ── the dev backend is a real MCP server ────────────────────────────


@pytest.mark.asyncio
async def test_the_dev_scaffold_backend_starts_and_answers_hello_over_mcp(tmp_path: Path) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    Scaffold(tmp_path, "acme.tickets").write_dev()
    params = StdioServerParameters(command=sys.executable, args=["server.py"], cwd=str(tmp_path))
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        assert [t.name for t in tools.tools] == ["hello"]
        result = await session.call_tool("hello", {})
    assert json.loads(result.content[0].text) == {"message": "hello from the extension backend"}


TUTORIAL = "https://github.com/ignite-ember/igni-sdk/blob/main/docs/YOUR_FIRST_EXTENSION.md"


def test_the_service_workspace_points_at_the_published_tutorial_and_says_how_the_sdks_install(
    tmp_path: Path,
) -> None:
    Scaffold(tmp_path / "t", "acme.tickets").write_service()
    pyproject = (tmp_path / "t" / "pyproject.toml").read_text()

    assert TUTORIAL in pyproject
    assert "Step 0" in pyproject
    assert "step 0" not in pyproject
    assert "docs/YOUR_FIRST_EXTENSION.md" not in pyproject.replace(TUTORIAL, "")
    assert "igni-extension and igni-service are not on PyPI" in pyproject
    assert "local wheels" in pyproject
    assert "--find-links" in pyproject
