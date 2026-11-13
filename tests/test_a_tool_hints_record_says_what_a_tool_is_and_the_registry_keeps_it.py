"""What a tool says about itself (:class:`ToolHints`) and where igni keeps it.

An extension's tools are classified by the four MCP annotations its manifest
declares, every one stated. A permission mode decides on them, so a hint that
was never stated must not be filled in, a contradictory set must not exist,
and a reinstall that drops a tool must drop its hints.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from igni_core.core.mcp.hints import ToolHints, ToolHintsRegistry

WIRE = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


def hints(**kw: bool) -> ToolHints:
    base = {"read_only": False, "destructive": False, "idempotent": False, "open_world": False}
    return ToolHints(**{**base, **kw})


class TestConstruction:
    def test_every_hint_is_required(self) -> None:
        for missing in WIRE:
            partial = {k: v for k, v in WIRE.items() if k != missing}
            with pytest.raises(ValidationError):
                ToolHints.model_validate(partial)

    def test_it_accepts_the_wire_spelling_and_the_python_one(self) -> None:
        by_wire = ToolHints.model_validate(WIRE)
        by_name = ToolHints(read_only=True, destructive=False, idempotent=True, open_world=False)

        assert by_wire == by_name

    def test_an_unknown_key_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            ToolHints.model_validate({**WIRE, "title": "x"})

    def test_it_is_frozen(self) -> None:
        h = hints()
        with pytest.raises(ValidationError):
            h.read_only = True  # type: ignore[misc]

    def test_a_tool_cannot_be_read_only_and_destructive(self) -> None:
        with pytest.raises(ValueError, match="read-only.*destructive"):
            hints(read_only=True, destructive=True)

    def test_a_non_boolean_is_refused_rather_than_coerced_from_text(self) -> None:
        with pytest.raises(ValidationError):
            ToolHints.model_validate({**WIRE, "readOnlyHint": "maybe"})


class TestWhatTheyMean:
    @pytest.mark.parametrize(
        ("read_only", "open_world", "expected"),
        [(True, False, True), (True, True, False), (False, False, False), (False, True, False)],
    )
    def test_a_local_read_reads_and_stays_inside(
        self, read_only: bool, open_world: bool, expected: bool
    ) -> None:
        assert hints(read_only=read_only, open_world=open_world).is_local_read is expected

    @pytest.mark.parametrize(
        ("read_only", "destructive", "open_world", "expected"),
        [
            (False, False, False, True),
            (True, False, False, False),
            (False, True, False, False),
            (False, False, True, False),
            (False, True, True, False),
        ],
    )
    def test_a_contained_write_changes_things_but_destroys_nothing_and_stays_inside(
        self, read_only: bool, destructive: bool, open_world: bool, expected: bool
    ) -> None:
        h = hints(read_only=read_only, destructive=destructive, open_world=open_world)

        assert h.is_contained_write is expected

    @pytest.mark.parametrize(
        ("kw", "line"),
        [
            ({"read_only": True}, "read-only, local"),
            ({"read_only": True, "idempotent": True}, "read-only, local, idempotent"),
            ({"read_only": True, "open_world": True}, "read-only, reaches outside"),
            ({}, "changes things, local"),
            ({"idempotent": True}, "changes things, local, idempotent"),
            ({"open_world": True}, "changes things, reaches outside"),
            ({"destructive": True, "open_world": True}, "destructive, reaches outside"),
            (
                {"destructive": True, "open_world": True, "idempotent": True},
                "destructive, reaches outside, idempotent",
            ),
            ({"destructive": True}, "destructive, local"),
        ],
    )
    def test_it_is_described_in_one_line(self, kw: dict[str, bool], line: str) -> None:
        assert hints(**kw).describe() == line


class TestWireSpelling:
    def test_the_wire_names_are_the_four_aliases_in_field_order(self) -> None:
        assert ToolHints.wire_names() == (
            "readOnlyHint",
            "destructiveHint",
            "idempotentHint",
            "openWorldHint",
        )

    def test_as_annotations_is_mcps_camel_case(self) -> None:
        assert ToolHints.model_validate(WIRE).as_annotations() == WIRE

    def test_a_round_trip_is_the_same_hints(self) -> None:
        original = hints(destructive=True, open_world=True)

        assert ToolHints.from_annotations(original.as_annotations()) == original


class TestReadingWhatAServerReports:
    def test_nothing_reported_is_none(self) -> None:
        assert ToolHints.from_annotations(None) is None

    def test_a_dict_of_four_is_read(self) -> None:
        assert ToolHints.from_annotations(WIRE) == ToolHints.model_validate(WIRE)

    def test_the_sdks_annotations_object_is_read(self) -> None:
        sdk = ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
        )

        assert ToolHints.from_annotations(sdk) == ToolHints.model_validate(WIRE)

    @pytest.mark.parametrize("key", list(WIRE))
    def test_a_missing_hint_is_an_absence_not_a_default(self, key: str) -> None:
        assert ToolHints.from_annotations({k: v for k, v in WIRE.items() if k != key}) is None

    @pytest.mark.parametrize("key", list(WIRE))
    def test_a_null_hint_is_an_absence_too(self, key: str) -> None:
        assert ToolHints.from_annotations({**WIRE, key: None}) is None

    def test_the_sdk_object_with_a_field_unset_is_an_absence(self) -> None:
        assert ToolHints.from_annotations(ToolAnnotations(readOnlyHint=True)) is None

    def test_a_contradiction_is_not_believed(self) -> None:
        both = {**WIRE, "readOnlyHint": True, "destructiveHint": True}

        assert ToolHints.from_annotations(both) is None

    def test_a_value_that_is_not_a_boolean_is_not_believed(self) -> None:
        assert ToolHints.from_annotations({**WIRE, "idempotentHint": "yes please"}) is None

    def test_keys_it_does_not_know_are_ignored(self) -> None:
        assert ToolHints.from_annotations({**WIRE, "title": "Search"}) == ToolHints.model_validate(
            WIRE
        )


class TestTheRegistry:
    def test_shared_is_one_instance_even_when_asked_from_many_threads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ToolHintsRegistry, "_shared", None)
        seen: list[ToolHintsRegistry] = []
        barrier = threading.Barrier(8)

        def ask() -> None:
            barrier.wait()
            seen.append(ToolHintsRegistry.shared())

        threads = [threading.Thread(target=ask) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(seen) == 8
        assert all(r is seen[0] for r in seen)
        assert ToolHintsRegistry.shared() is seen[0]

    def test_a_declared_tool_is_found_by_its_registered_name(self) -> None:
        reg = ToolHintsRegistry()
        read = hints(read_only=True)
        reg.declare("ext:acme", {"mcp_ext:acme.a_search": read})

        assert reg.get("mcp_ext:acme.a_search") is read

    def test_a_tool_igni_classifies_itself_has_none(self) -> None:
        assert ToolHintsRegistry().get("shell") is None

    def test_declaring_again_replaces_what_the_server_said_before(self) -> None:
        reg = ToolHintsRegistry()
        reg.declare("ext:acme", {"a": hints(read_only=True), "b": hints()})

        newer = hints(destructive=True)
        reg.declare("ext:acme", {"a": newer})

        assert reg.get("a") is newer
        assert reg.get("b") is None

    def test_forgetting_a_server_removes_only_its_tools(self) -> None:
        reg = ToolHintsRegistry()
        reg.declare("one", {"a": hints()})
        reg.declare("two", {"b": hints(read_only=True)})

        reg.forget("one")

        assert reg.get("a") is None
        assert reg.get("b") is not None

    def test_forgetting_a_server_nobody_declared_is_harmless(self) -> None:
        reg = ToolHintsRegistry()
        reg.declare("one", {"a": hints()})

        reg.forget("nobody")

        assert reg.get("a") is not None

    def test_two_servers_declaring_one_name_lose_it_when_either_is_forgotten(self) -> None:
        reg = ToolHintsRegistry()
        reg.declare("one", {"shared": hints()})
        reg.declare("two", {"shared": hints(read_only=True)})

        assert reg.get("shared") is not None
        reg.forget("one")

        assert reg.get("shared") is None

    def test_concurrent_declares_and_reads_never_see_a_half_replaced_server(self) -> None:
        reg = ToolHintsRegistry()
        names = [f"t{i}" for i in range(50)]
        stop = threading.Event()
        torn: list[Any] = []

        def reader() -> None:
            while not stop.is_set():
                present = {n for n in names if reg.get(n) is not None}
                if present and len(present) != len(names):
                    torn.append(present)

        t = threading.Thread(target=reader)
        t.start()
        for _ in range(300):
            reg.declare("s", {n: hints() for n in names})
            reg.forget("s")
        stop.set()
        t.join()

        assert torn == []
