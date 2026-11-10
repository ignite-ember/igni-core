"""What the frontend is told about installed extensions.

Pydantic rather than dicts, because this is a wire shape the web
client reads field by field and ``wire-schema.json`` snapshots.

Deliberately not the manifest. A manifest holds things the frontend
has no business with — the packaging, the egress allowlist, the
sidecar image — and sending the whole thing would put every one of
them in a browser's memory and in anything that logs a WebSocket
frame. What goes out is what is needed to draw: who it is, what state
it is in, where its UI lives, and what it contributes.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from igni_core.extensions.manifest import (
    IndicatorContribution,
    MenuContribution,
    PageContribution,
    Setting,
)
from igni_core.extensions.registry import InstalledExtension
from igni_core.protocol.json_value import WireJson


class ExtensionPage(BaseModel):
    """A page an extension contributes to the client."""

    kind: str = Field(
        description="The page's kind, a reverse-DNS name unique across extensions, "
        "e.g. `com.example.notes`."
    )
    label: str = Field(description="The page's title, for a person.")
    sidebar: bool = Field(default=False, description="Whether the page is listed in the sidebar.")
    entry: str = Field(
        default="index.html",
        description="The file, relative to the extension's `origin`, the page loads.",
    )


class ExtensionMenuEntry(BaseModel):
    """An entry an extension adds to the tools menu; choosing it runs a slash command."""

    label: str = Field(description="The entry's text, for a person.")
    command: str = Field(description="The slash command it runs, e.g. `/review`.")
    desc: str = Field(description="A one-line description of what it does.")


class ExtensionIndicator(BaseModel):
    """A small status indicator an extension shows in the client's own
    interface. The client gets its current value by calling a tool of
    the extension's every ``intervalSeconds``."""

    id: str = Field(description="The indicator's id, a reverse-DNS name.")
    label: str = Field(description="The indicator's name, for a person.")
    source: str = Field(
        description="The extension tool to call for the current value; it answers "
        "`{label, tone, detail}`."
    )
    opens: str = Field(
        default="",
        description="The `kind` of the extension page to open when the indicator is clicked; "
        "empty for an indicator that only reports.",
    )
    intervalSeconds: float = Field(
        default=30.0, description="How often to call `source`, in seconds."
    )


class ExtensionSetting(BaseModel):
    """One setting an extension can be configured with."""

    key: str = Field(description="The setting's key in `values`.")
    type: str = Field(description="The kind of value: `string`, `number`, `boolean` or `enum`.")
    label: str = Field(description="The setting's name, for a person.")
    default: WireJson = Field(default=None, description="The value used when none is set.")
    options: list[str] = Field(
        default_factory=list,
        description="The allowed values when `type` is `enum`; empty otherwise.",
    )


class ExtensionSwitchedOff(BaseModel):
    """One thing an extension does not get, because it collides with another."""

    claim: str = Field(
        description="What it claimed, e.g. `prompt:main.workflow`, `command:/review` "
        "or `agent:explorer`."
    )
    reason: str = Field(description="Who decided against it, or that nobody has decided yet.")


class ExtensionTool(BaseModel):
    """One tool an extension gives the agent, as the panel lists it."""

    name: str = Field(description="The tool's name.")
    hints: str = Field(
        description="What the tool says about itself, for a person, e.g. "
        "`read-only, local, idempotent`."
    )


class ExtensionSummary(BaseModel):
    """One installed extension: who it is, what state it is in, where its
    pages are served, and what it contributes."""

    # Deliberately not the manifest: packaging, the egress allowlist and the
    # sidecar image stay on the server. `allow` and `host` are copies for
    # display and early refusal; igni re-checks every request.
    id: str = Field(description="The extension's id, a reverse-DNS name.")
    name: str = Field(description="The extension's display name.")
    version: str = Field(description="The installed version, a semantic version.")
    state: str = Field(
        description="What the extension is doing: `READY`, `PREPARING`, `DISABLED` or `FAILED`."
    )
    reason: str = Field(
        default="", description="Why it is not `READY`, and what to do; empty when it is."
    )
    origin: str = Field(
        default="", description="Where its pages are served from; empty when it contributes none."
    )
    allow: list[str] = Field(
        default_factory=list,
        description="The RPC methods its pages were granted. igni checks every call; "
        "this copy lets a client refuse early.",
    )
    pages: list[ExtensionPage] = Field(default_factory=list, description="Pages it contributes.")
    menu: list[ExtensionMenuEntry] = Field(
        default_factory=list, description="Entries it adds to the tools menu."
    )
    indicators: list[ExtensionIndicator] = Field(
        default_factory=list, description="Status indicators it contributes."
    )
    settings: list[ExtensionSetting] = Field(
        default_factory=list,
        description="The settings it can be configured with; their current values are in `values`.",
    )
    values: dict[str, WireJson] = Field(
        default_factory=dict,
        description="The current setting values, by key, with defaults filled in and "
        "undeclared keys dropped. They reach its pages as launch parameters.",
    )
    host: list[str] = Field(
        default_factory=list,
        description="The host permissions it was granted, for display.",
    )
    web_url: str | None = Field(
        default=None, description="Its home on the web, when one is known; `null` otherwise."
    )
    service_url: str | None = Field(
        default=None,
        description="The URL of its own service, as its pages reach it; `null` when it has "
        "none a page may reach.",
    )
    switched_off: list[ExtensionSwitchedOff] = Field(
        default_factory=list,
        description="What it does not get because it collides with another extension. "
        "Usually empty.",
    )
    dropped: list[ExtensionSwitchedOff] = Field(
        default_factory=list,
        description="Its prompt edits that have no effect, because another extension "
        "replaces that prompt and has no section the edit names.",
    )
    problems: list[str] = Field(
        default_factory=list,
        description="Problems that do not stop it working, e.g. a tool reporting other "
        "hints than its manifest declares.",
    )
    faulty: str = Field(
        default="",
        description="Why it is marked faulty (its calls timed out or crashed too often in a "
        "row, and igni restarted it); empty while it answers.",
    )
    sandbox: str = Field(
        default="",
        description="How its process runs, e.g. `sandboxed (seatbelt)` or "
        "`not sandboxed: <why>`; empty when igni starts no process for it. "
        "An unsandboxed process has the user's full file and network access.",
    )
    tools: list[ExtensionTool] = Field(
        default_factory=list, description="The tools it gives the agent."
    )

    @classmethod
    def of(
        cls,
        entry: InstalledExtension,
        origin: str | None,
        values: dict[str, Any] | None = None,
        web_url: str | None = None,
        service_url: str | None = None,
    ) -> ExtensionSummary:
        manifest = entry.manifest
        return cls(
            id=manifest.id,
            name=manifest.display_name,
            version=manifest.version,
            state=entry.state.value,
            reason=entry.reason,
            origin=origin or "",
            allow=list(manifest.permissions.rpc),
            pages=[_page(p) for p in manifest.contributes.pages],
            menu=[_menu(m) for m in manifest.contributes.menu],
            indicators=[_indicator(i) for i in manifest.contributes.indicators],
            settings=[_setting(s) for s in manifest.contributes.settings],
            values=dict(values or {}),
            host=list(manifest.permissions.host),
            web_url=web_url,
            service_url=service_url,
            switched_off=[
                ExtensionSwitchedOff(claim=off.claim, reason=off.reason)
                for off in entry.switched_off
            ],
            dropped=[
                ExtensionSwitchedOff(claim=off.claim, reason=off.reason) for off in entry.dropped
            ],
            problems=list(entry.problems),
            faulty=entry.faulty,
            sandbox=entry.sandbox,
            tools=[
                ExtensionTool(name=tool.name, hints=tool.hints.describe())
                for tool in manifest.permissions.tools
            ],
        )


def _page(page: PageContribution) -> ExtensionPage:
    return ExtensionPage(kind=page.kind, label=page.label, sidebar=page.sidebar, entry=page.entry)


def _menu(entry: MenuContribution) -> ExtensionMenuEntry:
    return ExtensionMenuEntry(label=entry.label, command=entry.command, desc=entry.desc)


class PageTokenReply(BaseModel):
    """An access token an extension page uses to call its own service.

    When there is none to give (nobody is signed in, or the extension
    has no service), ``ok`` is false and ``error`` says why.
    """

    ok: bool = Field(description="Whether a token was issued.")
    access_token: str = Field(default="", description="The token; empty when `ok` is false.")
    expires_in: int = Field(
        default=0,
        description="How long the token stays valid, in seconds; renew it shortly before.",
    )
    service_url: str = Field(default="", description="The URL of the service the token is for.")
    error: str = Field(default="", description="Why no token was issued; empty when `ok` is true.")


class ExtensionListResult(BaseModel):
    """Every installed extension, in a stable order."""

    extensions: list[ExtensionSummary] = Field(
        default_factory=list, description="The installed extensions."
    )


class SetExtensionSettingsArgs(BaseModel):
    """New setting values for one extension."""

    extension_id: str = Field(description="The id of the extension to configure.")
    values: dict[str, WireJson] = Field(
        description="Setting key to new value, merged over the stored values. A key the "
        "extension does not declare is refused."
    )


def _setting(setting: Setting) -> ExtensionSetting:
    return ExtensionSetting(
        key=setting.key,
        type=setting.type,
        label=setting.label,
        default=setting.default,
        options=list(setting.options),
    )


def _indicator(indicator: IndicatorContribution) -> ExtensionIndicator:
    return ExtensionIndicator(
        id=indicator.id,
        label=indicator.label,
        source=indicator.source,
        opens=indicator.opens or "",
        intervalSeconds=indicator.intervalSeconds,
    )
