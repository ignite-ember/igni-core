"""`ExtensionSummary.of` maps an installed extension, field by field, onto
what the panel reads.

The exact field set (nothing of the manifest leaks) is pinned through the
real RPC in ``test_the_frontend_can_draw_an_extension.py``; this file pins
what each field holds, including the fallbacks for what is absent.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_core.backend.schemas_extensions import (
    ExtensionIndicator,
    ExtensionListResult,
    ExtensionPage,
    ExtensionSummary,
    PageTokenReply,
    SetExtensionSettingsArgs,
)
from igni_core.extensions.impact import SwitchedOff
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.registry import ExtensionState, InstalledExtension

READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
WRITES_OUTSIDE = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": True,
}


def manifest(**contributes) -> ExtensionManifest:
    return ExtensionManifest.model_validate(
        {
            "id": "acme.notes",
            "version": "2.3.4",
            "contract": ">=1.0 <2",
            "trust": "unsigned",
            "dev": {"command": ["python", "server.py"]},
            "permissions": {
                "rpc": ["ext.acme.notes.*"],
                "host": ["chat.read", "ui"],
                "tools": [
                    {"name": "notes_search", "annotations": READ_ONLY},
                    {"name": "notes_purge", "annotations": WRITES_OUTSIDE},
                ],
            },
            "contributes": contributes,
        }
    )


def rich() -> ExtensionManifest:
    return manifest(
        pages=[
            {"kind": "acme.notes.home", "label": "Notes", "sidebar": True, "entry": "app.html"},
            {"kind": "acme.notes.side", "label": "Side"},
        ],
        menu=[{"label": "Review", "command": "/review", "desc": "Review the notes"}],
        indicators=[
            {"id": "acme.notes.fresh", "label": "Fresh", "source": "notes_status"},
            {
                "id": "acme.notes.sync",
                "label": "Sync",
                "source": "notes_sync",
                "opens": "acme.notes.home",
                "intervalSeconds": 5,
            },
        ],
        settings=[
            {"key": "mode", "type": "enum", "label": "Mode", "options": ["a", "b"], "default": "a"},
            {"key": "limit", "type": "number", "label": "Limit", "default": 10},
            {"key": "title", "type": "string", "label": "Title"},
        ],
    )


class TestWhatIsCopiedFromTheManifest:
    def test_identity_state_and_grants(self):
        entry = InstalledExtension(
            manifest=rich(), state=ExtensionState.FAILED, reason="the process died"
        )

        summary = ExtensionSummary.of(entry, "http://127.0.0.1:9")

        assert (summary.id, summary.name, summary.version) == ("acme.notes", "acme.notes", "2.3.4")
        assert summary.state == "FAILED"
        assert summary.reason == "the process died"
        assert summary.origin == "http://127.0.0.1:9"
        assert summary.allow == ["ext.acme.notes.*"]
        assert summary.host == ["chat.read", "ui"]

    def test_a_named_extension_shows_its_name(self):
        named = rich().model_copy(update={"name": "Notes Pro"})

        assert ExtensionSummary.of(InstalledExtension(manifest=named), None).name == "Notes Pro"

    def test_pages_and_menu(self):
        summary = ExtensionSummary.of(InstalledExtension(manifest=rich()), None)

        assert [p.model_dump() for p in summary.pages] == [
            {"kind": "acme.notes.home", "label": "Notes", "sidebar": True, "entry": "app.html"},
            {"kind": "acme.notes.side", "label": "Side", "sidebar": False, "entry": "index.html"},
        ]
        assert [m.model_dump() for m in summary.menu] == [
            {"label": "Review", "command": "/review", "desc": "Review the notes"}
        ]

    def test_an_indicator_without_opens_says_empty_and_polls_every_thirty_seconds(self):
        summary = ExtensionSummary.of(InstalledExtension(manifest=rich()), None)

        fresh, sync = summary.indicators
        assert fresh.model_dump() == {
            "id": "acme.notes.fresh",
            "label": "Fresh",
            "source": "notes_status",
            "opens": "",
            "intervalSeconds": 30.0,
        }
        assert sync.opens == "acme.notes.home"
        assert sync.intervalSeconds == 5.0

    def test_settings_keep_type_default_and_options(self):
        summary = ExtensionSummary.of(InstalledExtension(manifest=rich()), None)

        assert [s.model_dump() for s in summary.settings] == [
            {"key": "mode", "type": "enum", "label": "Mode", "default": "a", "options": ["a", "b"]},
            {"key": "limit", "type": "number", "label": "Limit", "default": 10, "options": []},
            {"key": "title", "type": "string", "label": "Title", "default": None, "options": []},
        ]

    def test_each_tool_carries_the_sentence_its_hints_describe(self):
        summary = ExtensionSummary.of(InstalledExtension(manifest=rich()), None)

        assert [t.model_dump() for t in summary.tools] == [
            {"name": "notes_search", "hints": "read-only, local, idempotent"},
            {"name": "notes_purge", "hints": "destructive, reaches outside"},
        ]


class TestWhatIsCopiedFromTheState:
    def test_collisions_drops_problems_faults_and_sandbox(self):
        entry = InstalledExtension(
            manifest=rich(),
            state=ExtensionState.READY,
            switched_off=[SwitchedOff(claim="command:/review", reason="acme.other has it")],
            dropped=[SwitchedOff(claim="prompt:main.workflow", reason="no such section")],
            problems=["notes_search reports other hints"],
            faulty="timed out 3 times in a row",
            sandbox="not sandboxed: no seatbelt",
        )

        summary = ExtensionSummary.of(entry, "o")

        assert [s.model_dump() for s in summary.switched_off] == [
            {"claim": "command:/review", "reason": "acme.other has it"}
        ]
        assert [s.model_dump() for s in summary.dropped] == [
            {"claim": "prompt:main.workflow", "reason": "no such section"}
        ]
        assert summary.problems == ["notes_search reports other hints"]
        assert summary.faulty == "timed out 3 times in a row"
        assert summary.sandbox == "not sandboxed: no seatbelt"

    def test_a_healthy_extension_has_all_of_those_empty(self):
        summary = ExtensionSummary.of(InstalledExtension(manifest=rich()), None)

        assert summary.switched_off == []
        assert summary.dropped == []
        assert summary.problems == []
        assert summary.faulty == ""
        assert summary.sandbox == ""


class TestWhatTheCallerGives:
    def test_no_origin_values_or_urls_become_empty_empty_and_null(self):
        summary = ExtensionSummary.of(InstalledExtension(manifest=rich()), None)

        assert summary.origin == ""
        assert summary.values == {}
        assert summary.web_url is None
        assert summary.service_url is None

    def test_the_urls_and_values_are_carried(self):
        summary = ExtensionSummary.of(
            InstalledExtension(manifest=rich()),
            "o",
            values={"mode": "b", "limit": None},
            web_url="https://notes.example",
            service_url="https://svc.example/api",
        )

        assert summary.values == {"mode": "b", "limit": None}
        assert summary.web_url == "https://notes.example"
        assert summary.service_url == "https://svc.example/api"

    def test_an_extension_that_contributes_and_grants_nothing_has_empty_lists(self):
        bare = ExtensionManifest.model_validate(
            {
                "id": "acme.bare",
                "version": "1.0.0",
                "contract": ">=1.0 <2",
                "trust": "unsigned",
                "dev": {"command": ["python", "x.py"]},
            }
        )

        summary = ExtensionSummary.of(InstalledExtension(manifest=bare), None)

        assert (summary.pages, summary.menu, summary.indicators, summary.settings) == (
            [],
            [],
            [],
            [],
        )
        assert (summary.allow, summary.host, summary.tools) == ([], [], [])


class TestTheWireShapes:
    def test_a_page_defaults_to_index_html_outside_the_sidebar(self):
        page = ExtensionPage(kind="a.b", label="L")

        assert (page.sidebar, page.entry) == (False, "index.html")

    def test_an_indicator_defaults(self):
        chip = ExtensionIndicator(id="a.b", label="L", source="s")

        assert (chip.opens, chip.intervalSeconds) == ("", 30.0)

    def test_a_token_reply_that_refused_carries_only_the_reason(self):
        assert PageTokenReply(ok=False, error="not signed in").model_dump() == {
            "ok": False,
            "access_token": "",
            "expires_in": 0,
            "service_url": "",
            "error": "not signed in",
        }

    def test_a_token_reply_that_issued_carries_token_lifetime_and_service(self):
        reply = PageTokenReply(ok=True, access_token="t", expires_in=300, service_url="https://s")

        assert reply.model_dump() == {
            "ok": True,
            "access_token": "t",
            "expires_in": 300,
            "service_url": "https://s",
            "error": "",
        }

    def test_the_list_result_keeps_the_order_it_is_given_and_defaults_to_none(self):
        assert ExtensionListResult().extensions == []
        first = ExtensionSummary.of(InstalledExtension(manifest=rich()), None)
        second = first.model_copy(update={"id": "acme.zzz"})

        listed = ExtensionListResult(extensions=[second, first])

        assert [e.id for e in listed.extensions] == ["acme.zzz", "acme.notes"]

    def test_the_settings_args_take_json_values_and_need_both_fields(self):
        ok = SetExtensionSettingsArgs(
            extension_id="acme.notes", values={"a": 1, "b": [True, None], "c": {"d": "é"}}
        )
        assert ok.values["c"] == {"d": "é"}

        with pytest.raises(ValidationError):
            SetExtensionSettingsArgs(extension_id="acme.notes")  # type: ignore[call-arg]
