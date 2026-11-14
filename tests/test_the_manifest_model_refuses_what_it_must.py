"""The manifest model's own rules, one by one, through real validation."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from igni_core.extensions.manifest import ExtensionManifest

HINTS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
DEV = {"command": ["python", "server.py"]}
BASE = {
    "id": "acme.tickets",
    "version": "0.1.0",
    "contract": ">=1.0 <2",
    "trust": "unsigned",
    "dev": DEV,
}


def _make(**changes):
    data = copy.deepcopy(BASE)
    data.update(copy.deepcopy(changes))
    return ExtensionManifest.model_validate(data)


def _refusal(**changes) -> str:
    with pytest.raises(ValidationError) as caught:
        _make(**changes)
    return str(caught.value)


# ── packaging ─────────────────────────────────────────────────────


def test_no_packaging_is_refused_saying_none_was_given():
    data = {k: v for k, v in BASE.items() if k != "dev"}
    with pytest.raises(ValidationError, match="and none was given"):
        ExtensionManifest.model_validate(data)


def test_two_packagings_are_refused_naming_both():
    message = _refusal(endpoint={"url": "https://x.example"})
    assert "exactly one of" in message
    assert "endpoint, dev was given" in message


@pytest.mark.parametrize(
    ("packaging", "extra"),
    [
        ("sidecar", {"sidecar": {"image": "x:1", "mcp": {"transport": "stdio"}}}),
        ("endpoint", {"endpoint": {"url": "https://x.example"}}),
        ("python", {"python": {"package": "p", "entry": "e", "mcp": {"transport": "stdio"}}}),
    ],
)
def test_each_packaging_names_itself(packaging, extra):
    data = {k: v for k, v in BASE.items() if k != "dev"}
    manifest = ExtensionManifest.model_validate({**data, **extra})
    assert manifest.packaging == packaging


def test_dev_packaging_names_itself():
    assert _make().packaging == "dev"


def test_a_dev_command_is_a_development_command_whatever_the_trust():
    assert _make().is_development_command is True
    python = {"python": {"package": "p", "entry": "e", "mcp": {"transport": "stdio"}}}
    data = {k: v for k, v in BASE.items() if k != "dev"}
    assert ExtensionManifest.model_validate({**data, **python}).is_development_command is False


@pytest.mark.parametrize("trust", ["partner", "first-party"])
def test_a_dev_command_on_a_labelled_trust_is_refused(trust):
    message = _refusal(trust=trust)
    assert f"a 'dev' command needs trust 'unsigned', not {trust!r}" in message


def test_only_an_endpoint_does_not_run_here():
    data = {k: v for k, v in BASE.items() if k != "dev"}
    endpoint = ExtensionManifest.model_validate({**data, "endpoint": {"url": "https://x.example"}})
    assert endpoint.runs_here is False
    assert _make().runs_here is True


# ── hooks and tools ───────────────────────────────────────────────


def test_hooks_on_an_endpoint_are_refused():
    data = {k: v for k, v in BASE.items() if k != "dev"}
    with pytest.raises(ValidationError, match="needs a process igni starts"):
        ExtensionManifest.model_validate(
            {
                **data,
                "endpoint": {"url": "https://x.example"},
                "permissions": {"hooks": ["PreToolUse"]},
            }
        )


def test_hooks_on_a_process_igni_starts_are_accepted():
    assert _make(permissions={"hooks": ["PreToolUse", "Stop"]}).permissions.hooks


def test_a_hook_event_named_twice_is_refused():
    message = _refusal(permissions={"hooks": ["Stop", "Stop"]})
    assert "permissions.hooks names Stop more than once" in message


def test_a_tool_named_twice_is_refused():
    tool = {"name": "acme.tickets.list", "annotations": HINTS}
    message = _refusal(permissions={"tools": [tool, tool]})
    assert "permissions.tools names acme.tickets.list more than once" in message


def test_tool_names_lists_them_in_order():
    tools = [{"name": n, "annotations": HINTS} for n in ("acme.tickets.b", "acme.tickets.a")]
    assert _make(permissions={"tools": tools}).permissions.tool_names() == [
        "acme.tickets.b",
        "acme.tickets.a",
    ]


def test_a_tool_must_state_every_hint():
    partial = {"name": "acme.tickets.list", "annotations": {"readOnlyHint": True}}
    assert "openWorldHint" in _refusal(permissions={"tools": [partial]})


# ── grants and egress ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("rpc", "method", "granted"),
    [
        (["ext.acme.list"], "ext.acme.list", True),
        (["ext.acme.list"], "ext.acme.list2", False),
        (["ext.acme.*"], "ext.acme.anything", True),
        (["ext.acme.*"], "ext.other.anything", False),
        (["ext.acme"], "ext.acme.list", False),
        ([], "ext.acme.list", False),
    ],
)
def test_an_rpc_grant_is_exact_or_a_star_prefix(rpc, method, granted):
    assert _make(permissions={"rpc": rpc}).grants_rpc(method) is granted


def test_a_bare_star_is_not_a_grant_the_pattern_allows():
    assert "rpc" in _refusal(permissions={"rpc": ["*"]})


@pytest.mark.parametrize(
    "host", ["https://x.example", "x.example/path", "X.example", "x example", "x.example:"]
)
def test_egress_is_a_host_not_a_url(host):
    assert "egress" in _refusal(permissions={"egress": [host]})


def test_egress_takes_a_host_and_a_port():
    assert _make(permissions={"egress": ["x.example", "x.example:8443"]}).permissions.egress


# ── identity ──────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["Acme.tickets", "acme", "acme.", "ünï.tickets", "1acme.x"])
def test_an_id_is_lowercase_reverse_dns(bad):
    assert "id" in _refusal(id=bad)


@pytest.mark.parametrize("bad", ["1.0", "v1.0.0", "1.0.0.0", ""])
def test_a_version_is_semver(bad):
    assert "version" in _refusal(version=bad)


def test_a_prerelease_version_is_semver():
    assert _make(version="1.2.3-rc.1").version == "1.2.3-rc.1"


def test_trust_is_one_of_three_labels():
    assert "trust" in _refusal(trust="trusted")


def test_display_name_falls_back_to_the_id():
    assert _make().display_name == "acme.tickets"
    assert _make(name="").display_name == "acme.tickets"
    assert _make(name="Tickets").display_name == "Tickets"


def test_an_unknown_field_anywhere_is_refused():
    assert "Extra inputs are not permitted" in _refusal(surprise=1)
    assert "contributes.surprise" in _refusal(contributes={"surprise": 1})


def test_the_schema_key_and_alias_spellings_round_trip():
    manifest = _make(**{"$schema": "https://x.example/s.json", "vm": {"volumeMaxGib": 5}})
    dumped = manifest.model_dump(by_alias=True, exclude_none=True)
    assert dumped["$schema"] == "https://x.example/s.json"
    assert dumped["vm"]["volumeMaxGib"] == 5
    assert ExtensionManifest.model_validate(dumped) == manifest


# ── VM bounds ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("field", "low", "high"),
    [("volumeMaxGib", 1, 65536), ("eraseAfterIdleDays", 1, 3650), ("volumeGib", 1, 65536)],
)
def test_a_vm_bound_holds_exactly_at_its_edges(field, low, high):
    assert _make(vm={field: low}).vm is not None
    assert _make(vm={field: high}).vm is not None
    assert field in _refusal(vm={field: low - 1})
    assert field in _refusal(vm={field: high + 1})


def test_vm_snake_case_names_are_refused():
    assert _refusal(vm={"volume_gib": 5})


@pytest.mark.parametrize("package", ["", "a b", "a;b", "$(x)", "-x"])
def test_a_vm_package_is_one_word(package):
    assert _refusal(vm={"packages": {"apt": [package]}})


@pytest.mark.parametrize("package", ["chromium", "requests==2.32.3", "@playwright/mcp@0.0.84"])
def test_a_vm_package_may_be_versioned_or_scoped(package):
    assert _make(vm={"packages": {"npm": [package]}}).vm.packages.npm == [package]


# ── pages ─────────────────────────────────────────────────────────


def test_a_page_kind_must_be_namespaced():
    page = {"kind": "tickets", "label": "Tickets"}
    assert "is not namespaced" in _refusal(contributes={"pages": [page]})
    page["kind"] = "acme.tickets"
    assert _make(contributes={"pages": [page]}).contributes.pages[0].kind == "acme.tickets"


# ── prompts ───────────────────────────────────────────────────────


def _override(target="main", sections=("intro",), source="file", from_="o/main.md", **more):
    return {
        "target": target,
        "sections": list(sections),
        "source": source,
        "from": from_,
        **more,
    }


def _edit(target="main", from_="e.md", **change):
    return {"target": target, "source": "file", "from": from_, **change}


def _prompt(overrides=(), edits=()):
    return {"overrides": list(overrides), "edits": list(edits)}


def test_a_prompt_overridden_twice_is_refused():
    message = _refusal(contributes={"prompt": _prompt(overrides=[_override(), _override()])})
    assert "overrides main twice" in message


def test_an_override_naming_a_section_twice_is_refused():
    message = _refusal(
        contributes={"prompt": _prompt(overrides=[_override(sections=["intro", "intro"])])}
    )
    assert "override of main names intro twice" in message


def test_overriding_and_editing_one_prompt_is_refused():
    message = _refusal(
        contributes={"prompt": _prompt(overrides=[_override()], edits=[_edit(append="tail")])}
    )
    assert "both overrides and edits main" in message


def test_replacing_one_section_twice_is_refused():
    edits = [_edit(replace="intro"), _edit(replace="intro", from_="f.md")]
    assert "replaces main#intro twice" in _refusal(contributes={"prompt": _prompt(edits=edits)})


def test_adding_one_section_twice_is_refused():
    edits = [_edit(append="tail"), _edit(insert="tail", before="intro", from_="f.md")]
    assert "adds main#tail twice" in _refusal(contributes={"prompt": _prompt(edits=edits)})


def test_different_edits_of_one_prompt_stack():
    edits = [_edit(replace="intro"), _edit(append="tail", from_="f.md")]
    assert len(_make(contributes={"prompt": _prompt(edits=edits)}).contributes.prompt.edits) == 2


@pytest.mark.parametrize(
    ("change", "text"),
    [
        ({}, "it says none"),
        ({"replace": "a", "append": "b"}, "it says replace, append"),
        ({"insert": "a"}, "inserting a needs exactly one of 'before' and 'after'"),
        ({"insert": "a", "before": "b", "after": "c"}, "needs exactly one of"),
        ({"append": "a", "before": "b"}, "'before' places an insert"),
        ({"replace": "a", "after": "b"}, "'after' places an insert"),
    ],
)
def test_an_edit_is_exactly_one_change(change, text):
    assert text in _refusal(contributes={"prompt": _prompt(edits=[_edit(**change)])})


def test_an_insert_placed_before_or_after_is_accepted():
    edits = [_edit(insert="a", before="b"), _edit(insert="c", after="b", from_="f.md")]
    got = _make(contributes={"prompt": _prompt(edits=edits)}).contributes.prompt.edits
    assert [e.anchor for e in got] == ["b", "b"]
    assert [e.added for e in got] == ["a", "c"]


@pytest.mark.parametrize("plugin", ["plug", "/plug/", "plug/"])
@pytest.mark.parametrize("from_", ["plug/agents/x.md", "/plug/agents/x.md"])
def test_an_override_file_inside_the_plugin_agents_is_refused(plugin, from_):
    contributes = {"plugin": plugin, "prompt": _prompt(overrides=[_override(from_=from_)])}
    message = _refusal(contributes=contributes)
    assert "everything there loads as the plugin's own agent" in message
    assert "plug/overrides/" in message


def test_an_override_beside_the_agents_is_accepted():
    contributes = {"plugin": "plug", "prompt": _prompt(overrides=[_override(from_="plug/o/m.md")])}
    assert _make(contributes=contributes)


def test_a_tool_sourced_override_is_not_a_file_in_agents():
    override = _override(source="tool", from_="plug/agents/x")
    assert _make(contributes={"plugin": "plug", "prompt": _prompt(overrides=[override])})


@pytest.mark.parametrize("plugin", [None, "", "  ", "/"])
def test_with_no_plugin_the_override_path_is_not_checked(plugin):
    override = _override(from_="plug/agents/x.md")
    assert _make(contributes={"plugin": plugin, "prompt": _prompt(overrides=[override])})


# ── settings ──────────────────────────────────────────────────────


def test_an_enum_setting_without_options_is_refused():
    setting = {"key": "mode", "type": "enum", "label": "Mode"}
    assert "is an enum with no options" in _refusal(contributes={"settings": [setting]})
