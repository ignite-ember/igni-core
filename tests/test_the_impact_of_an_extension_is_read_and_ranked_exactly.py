"""The small rules under the collision table: claim spelling, rank, igni's commands.

The fixture cases (``test_extensions_collide_and_the_higher_priority_wins``)
pin whole outcomes. These pin the pieces an outcome is built from, at
their edges: a claim with an odd name, an extension the order does not
name, igni's commands written with a slash and capitals, an empty set.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.core.mcp.hints import ToolHints
from igni_core.extensions.impact import (
    Claim,
    ExtensionImpact,
    IgniCommands,
    ImpactEdit,
    ImpactOverride,
    ImpactResolver,
    ImpactTool,
    Priority,
)
from igni_core.extensions.manifest import ExtensionManifest


def impact(extension_id: str, **contributes: object) -> ExtensionImpact:
    return ExtensionImpact.of(
        ExtensionManifest.model_validate(
            {
                "id": extension_id,
                "version": "1.0.0",
                "contract": ">=1.0 <2",
                "trust": "unsigned",
                "dev": {"command": ["true"]},
                "contributes": contributes,
            }
        )
    )


def command(name: str) -> dict[str, str]:
    return {"command": name, "desc": "x"}


# ── Claims ────────────────────────────────────────────────────────


def test_claims_are_spelled_kind_colon_name() -> None:
    assert Claim.prompt("main") == "prompt:main"
    assert Claim.prompt("main#workflow") == "prompt:main#workflow"
    assert Claim.addition("agent:explorer", "notes") == "prompt:agent:explorer+notes"
    assert Claim.command("/a") == "command:/a"
    assert Claim.anchor("A_SCHEMA") == "anchor:A_SCHEMA"


@pytest.mark.parametrize(
    ("claim", "whole"),
    [
        ("prompt:main", "main"),
        ("prompt:agent:explorer", "agent:explorer"),
        ("prompt:main#workflow", None),
        ("prompt:main+notes", None),
        ("command:/a", None),
        ("anchor:X", None),
        ("main", None),
        ("", None),
        ("prompt:", ""),
    ],
)
def test_only_a_plain_prompt_claim_names_a_whole_prompt(claim: str, whole: str | None) -> None:
    assert Claim.whole_prompt(claim) == whole


@pytest.mark.parametrize(
    ("item", "document", "edit"),
    [
        ("prompt:main", "main", False),
        ("prompt:main#workflow", "main", True),
        ("prompt:main+notes", "main", True),
        ("prompt:agent:explorer+notes", "agent:explorer", True),
        ("prompt:main#a+b", "main", True),
        ("command:/a#b", None, False),
        ("anchor:A+B", None, False),
        ("", None, False),
    ],
)
def test_a_claim_names_its_prompt_and_says_whether_it_edits(
    item: str, document: str | None, edit: bool
) -> None:
    assert Claim.prompt_document(item) == document
    assert Claim.is_edit(item) is edit


# ── An override, an edit, a tool ──────────────────────────────────


def test_an_agent_override_has_its_frontmatter_without_listing_it() -> None:
    agent = ImpactOverride(target="agent:explorer", sections=["intro"])
    main = ImpactOverride(target="main", sections=["identity"])

    assert agent.has("intro") and agent.has("frontmatter")
    assert not agent.has("outro")
    assert main.has("identity")
    assert not main.has("frontmatter")


def test_an_edit_is_keyed_by_the_section_it_replaces_or_the_name_it_adds() -> None:
    found = impact(
        "acme.a",
        prompt={
            "edits": [
                {"target": "main", "replace": "skip", "source": "file", "from": "a.md"},
                {
                    "target": "main",
                    "insert": "extra",
                    "before": "intro",
                    "source": "file",
                    "from": "b.md",
                },
                {"target": "main", "append": "tail", "source": "file", "from": "c.md"},
            ]
        },
    )

    replace, insert, append = found.prompt_edits
    assert (replace.key, insert.key, append.key) == (
        "prompt:main#skip",
        "prompt:main+extra",
        "prompt:main+tail",
    )
    assert insert.position.value == "before" and insert.section == "intro"
    assert append.section is None and append.position is None
    assert isinstance(replace, ImpactEdit)


def test_a_tool_carries_the_four_hints_under_mcp_s_names() -> None:
    hints = ToolHints(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
    )

    assert ImpactTool.of("acme.a.drop", hints).model_dump() == {
        "name": "acme.a.drop",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }


# ── Reading a manifest ────────────────────────────────────────────


def test_what_a_manifest_lists_is_read_off_sorted() -> None:
    found = impact(
        "acme.a",
        commands=[command("/zed"), command("/alpha")],
        promptFragments=[
            {"anchor": "B", "source": "tool", "from": "x"},
            {"anchor": "A", "source": "tool", "from": "x"},
        ],
        prompt={
            "overrides": [
                {"target": "plan_mode", "sections": ["s"], "source": "file", "from": "p.md"},
                {"target": "main", "sections": ["s"], "source": "file", "from": "m.md"},
            ]
        },
    )

    assert found.commands == ["/alpha", "/zed"]
    assert found.anchors == ["A", "B"]
    assert [o.target for o in found.prompt_overrides] == ["main", "plan_mode"]
    assert found.claims() == sorted(found.claims())
    assert found.override("main") is not None and found.override("nope") is None
    assert found.edits_of("main") == []


# ── Priority ──────────────────────────────────────────────────────


def test_priority_ranks_the_named_then_the_unnamed_by_id() -> None:
    order = Priority(["acme.b", "acme.a"])

    assert order.ordered(["acme.z", "acme.a", "acme.y", "acme.b"]) == [
        "acme.b",
        "acme.a",
        "acme.y",
        "acme.z",
    ]
    assert order.above("acme.b", "acme.a")
    assert not order.above("acme.a", "acme.b")
    assert order.above("acme.a", "acme.y")
    assert order.above("acme.y", "acme.z")
    assert not order.above("acme.y", "acme.y")


def test_an_empty_order_ranks_everything_by_id() -> None:
    assert Priority().ordered(["b", "a"]) == ["a", "b"]


# ── igni's commands ───────────────────────────────────────────────


def test_igni_commands_are_bare_lowercase_unique_and_sorted() -> None:
    commands = IgniCommands.of(["/Clear", "clear", "/HELP", "/compact"])

    assert commands.commands == ["clear", "compact", "help"]
    assert commands.claims() == ["command:/clear", "command:/compact", "command:/help"]


def test_igni_commands_are_read_from_a_json_file(tmp_path: Path) -> None:
    path = tmp_path / "commands.json"
    path.write_text('{"commands": ["clear", "help"]}', encoding="utf-8")

    assert IgniCommands.load(path).commands == ["clear", "help"]


def test_a_missing_or_malformed_commands_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        IgniCommands.load(tmp_path / "absent.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid JSON"):
        IgniCommands.load(bad)


def test_the_clashes_are_the_declared_commands_that_are_igni_s() -> None:
    found = impact("acme.a", commands=[command("/clear"), command("/mine")])

    assert IgniCommands.of(["Clear", "help"]).clashes(found) == ["/clear"]
    assert IgniCommands.of([]).clashes(found) == []


# ── Resolving ─────────────────────────────────────────────────────


def test_nothing_collides_among_no_extensions_or_one() -> None:
    assert ImpactResolver([]).resolve().collisions == []

    alone = impact("acme.a", commands=[command("/a")])
    outcome = ImpactResolver([alone], ["acme.a"]).resolve()
    assert outcome.collisions == [] and outcome.off == {} and outcome.dropped == {}


def test_an_extension_the_order_does_not_name_loses_to_one_it_does() -> None:
    named = impact("acme.zz", commands=[command("/x")])
    unnamed = impact("acme.aa", commands=[command("/x")])

    outcome = ImpactResolver([unnamed, named], ["acme.zz"]).resolve()

    (collision,) = outcome.collisions
    assert collision.winner == "acme.zz" and collision.losers == ["acme.aa"]
    assert [o.reason for o in outcome.off["acme.aa"]] == [
        "lower priority than acme.zz, which also claims command:/x"
    ]


def test_three_extensions_on_one_claim_leave_one_winner_and_two_losers() -> None:
    impacts = [impact(name, commands=[command("/x")]) for name in ("acme.a", "acme.b", "acme.c")]

    outcome = ImpactResolver(impacts, ["acme.b", "acme.c", "acme.a"]).resolve()

    (collision,) = outcome.collisions
    assert (collision.winner, collision.losers) == ("acme.b", ["acme.a", "acme.c"])
    assert collision.extensions == ["acme.a", "acme.b", "acme.c"]
    assert outcome.involving("acme.a") == [collision]
    assert outcome.involving("acme.other") == []
    assert outcome.allows("acme.b", "command:/x")
    assert not outcome.allows("acme.c", "command:/x")


def test_an_igni_command_is_switched_off_with_igni_s_own_reason() -> None:
    reserved = IgniCommands.of(["/Clear"]).claims()
    exact = impact("acme.b", commands=[command("/clear"), command("/mine")])

    outcome = ImpactResolver([exact], reserved=reserved).resolve()

    off = outcome.off["acme.b"]
    assert [o.claim for o in off] == ["command:/clear"]
    assert off[0].reason.startswith("command:/clear is igni's own")
    assert not outcome.allows("acme.b", "command:/clear")
    assert outcome.allows("acme.b", "command:/mine")


def test_a_service_grant_listed_twice_is_read_once() -> None:
    found = ExtensionImpact.of(
        ExtensionManifest.model_validate(
            {
                "id": "acme.a",
                "version": "1.0.0",
                "contract": ">=1.0 <2",
                "trust": "unsigned",
                "dev": {"command": ["true"]},
                "permissions": {"service": ["messages", "chats", "messages"]},
            }
        )
    )

    assert found.service == ["chats", "messages"]
