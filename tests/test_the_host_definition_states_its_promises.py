"""What ``definition.py`` promises, held against the real schema.

The host schema is a declarative table, so what it promises is read off
the table itself: the version the published file carries, the document
allow-list ``host.workspace.open`` hands to the host, the types that
carry no credential, the members that are typed, and the members still
written ``json``.
"""

from __future__ import annotations

import re

import pytest

from igni_core.extensions.host import published
from igni_core.extensions.host.definition import (
    HOST_SCHEMA,
    OPENABLE_SUFFIXES,
    PERMISSIONS,
    SCHEMA_VERSION,
)
from igni_core.extensions.host.spec import HostSchema, MethodSpec, ObjectSpec, TypeSpec


def _type(name: str) -> TypeSpec:
    found = HOST_SCHEMA.type(name)
    assert found is not None, name
    return found


def _fields(name: str) -> dict[str, str]:
    return {f.name: f.type for f in _type(name).fields}


def _words(expression: str) -> list[str]:
    return re.findall(r"[A-Za-z_]+", expression)


def test_the_schema_is_version_1_13_0() -> None:
    assert SCHEMA_VERSION == "1.13.0"
    assert HOST_SCHEMA.version == "1.13.0"
    assert HostSchema.model_validate_json(published.PUBLISHED.read_text()).version == "1.13.0"


def test_only_documents_open_outside_the_sandbox() -> None:
    assert {
        "csv",
        "gif",
        "htm",
        "html",
        "jpeg",
        "jpg",
        "md",
        "pdf",
        "png",
        "svg",
        "txt",
        "webp",
    } == OPENABLE_SUFFIXES


@pytest.mark.parametrize("suffix", ["app", "sh", "exe", "zip", "command", "webloc", "terminal"])
def test_a_program_or_a_bundle_is_not_openable(suffix: str) -> None:
    assert suffix not in OPENABLE_SUFFIXES


def test_the_open_method_lists_the_suffixes_it_opens() -> None:
    _spec, member = HOST_SCHEMA.resolve("workspace.open")
    assert isinstance(member, MethodSpec)

    listed = re.findall(r"`\.([a-z]+)`", member.doc)

    assert listed == sorted(OPENABLE_SUFFIXES)
    assert member.permission == "files.open"


def test_a_failed_update_check_is_documented_as_refused() -> None:
    _spec, member = HOST_SCHEMA.resolve("app.check_for_update")
    assert isinstance(member, MethodSpec)

    assert member.result == "UpdateInfo | None"
    assert "refused" in member.doc


def test_a_model_entry_never_carries_its_credential() -> None:
    fields = _fields("ModelEntry")

    assert fields["has_api_key"] == "bool"
    assert not [name for name in fields if "key" in name and name != "has_api_key"]
    assert not [name for name in fields if "env" in name or "command" in name]


def test_the_model_registry_is_typed() -> None:
    assert _fields("ModelRegistry") == {
        "default": "str",
        "max_context_window": "int",
        "registry": "dict[str, ModelEntry]",
    }
    _spec, member = HOST_SCHEMA.resolve("models.registry")
    assert member.type == "ModelRegistry"  # type: ignore[union-attr]


def test_plan_tasks_and_todos_are_lists_of_todo_items() -> None:
    assert _fields("Plan")["tasks"] == "list[TodoItem]"
    assert _fields("Todos")["todos"] == "list[TodoItem]"
    assert set(_fields("TodoItem")) == {"content", "status", "activeForm"}


def test_a_loop_says_how_many_iterations_were_announced() -> None:
    assert _fields("LoopStatus")["announced_total"] == "int | None"


def test_mcp_and_plugin_members_answer_with_types() -> None:
    answers = {
        "mcp.servers": "list[McpServer]",
        "mcp.status": "list[McpServer]",
        "mcp.details": "list[McpServerDetails]",
        "plugins.installed": "list[PluginInfo]",
        "plugins.marketplaces": "list[Marketplace]",
    }
    for path, type_ in answers.items():
        _spec, member = HOST_SCHEMA.resolve(path)
        assert member.type == type_, path  # type: ignore[union-attr]
    for path, type_ in {
        "mcp.ensure": "McpInit",
        "mcp.set_tool_enabled": "McpToolToggle",
        "plugins.contents": "PluginContents",
        "plugins.preview": "PluginContents",
    }.items():
        _spec, member = HOST_SCHEMA.resolve(path)
        assert member.result == type_, path  # type: ignore[union-attr]


def test_the_members_still_written_json_are_exactly_the_known_ones() -> None:
    untyped: list[str] = []
    for t in HOST_SCHEMA.types:
        untyped += [f"{t.name}.{f.name}" for f in t.fields if "json" in _words(f.type)]
    for o in HOST_SCHEMA.objects:
        untyped += [f"{o.path}.{p.name}" for p in o.properties if "json" in _words(p.type)]
        untyped += [f"{o.path}.{s.name}" for s in o.signals if "json" in _words(s.type)]
        for m in o.methods:
            if "json" in _words(m.result):
                untyped.append(f"{o.path}.{m.name}()")
            untyped += [
                f"{o.path}.{m.name}({p.name})" for p in m.params if "json" in _words(p.type)
            ]

    assert untyped == [
        "Change.value",
        "Panel.params",
        "Dialog.spec",
        "DialogChange.answer",
        "Approval.tool_args",
        "ContextUsage.breakdown",
        "AgentEvent.payload",
        "Retry.payload",
        "View.spec",
        "ViewAction.params",
        "PlanChange.payload",
        "TaskSnapshot.tasks",
        "CollectionChange.payload",
        "ProcessEvent.payload",
        "SettingsChange.values",
        "ModelEntry.thinking",
        "ui.open_panel(params)",
        "ui.show_dialog()",
        "ui.show_dialog(spec)",
        "chat.messages",
        "chat.pending",
        "chat.interrupted_runs",
        "chat.append(view)",
        "chat.replace(view)",
        "chat.search()",
        "chat.truncate()",
        "chat.discard_interrupted()",
        "chat.upload()",
        "chat.views.dispatch()",
        "chat.views.dispatch(params)",
        "workflows.run(input)",
        "workspace.read()",
        "workspace.list()",
        "workspace.complete()",
        "workspace.search()",
        "workspace.pick_dir()",
        "extensions.set_settings(values)",
        "permissions.check(tool_args)",
        "self.settings",
    ]


def test_every_object_asks_for_a_declared_grant() -> None:
    granted = {p.name for p in PERMISSIONS}
    objects: list[ObjectSpec] = list(HOST_SCHEMA.objects)

    asked = {o.permission for o in objects if o.permission}
    asked |= {m.permission for o in objects for m in o.methods if m.permission}
    asked |= {m.elsewhere for o in objects for m in o.methods if m.elsewhere}

    assert asked <= granted
    assert {"app", "models", "mcp", "plugins", "permissions", "files.open"} <= granted


def test_the_schema_names_no_old_product() -> None:
    text = published.render()

    assert not re.search(r"\bember\b|ignite-ember|ember_", text, re.IGNORECASE)
    assert "igni" in text


def test_a_schema_with_an_unknown_type_is_refused() -> None:
    broken = HOST_SCHEMA.model_dump()
    broken["types"][0]["fields"][0]["type"] = "NoSuchType"

    with pytest.raises(ValueError, match="unknown type 'NoSuchType'"):
        HostSchema.model_validate(broken)


def test_a_schema_with_an_undeclared_permission_is_refused() -> None:
    broken = HOST_SCHEMA.model_dump()
    broken["objects"][1]["permission"] = "no.such.grant"

    with pytest.raises(ValueError, match="no.such.grant"):
        HostSchema.model_validate(broken)
