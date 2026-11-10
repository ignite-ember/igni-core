"""Pydantic schemas shared between the plugin controllers.

* :class:`PluginPreviewKey` — frozen key for
  :attr:`PluginController._preview_cache`.
* :class:`PluginPreviewSource` — parsed ``url [subdir]`` display
  shape used by the marketplace panel.
* :class:`PluginSkillInfo` / :class:`PluginAgentInfo` /
  :class:`PluginHookInfo` / :class:`PluginMCPServerInfo` /
  :class:`PluginToolInfo` — per-item wire entries inside
  :class:`PluginContents`.
* :class:`PluginContents` — plugin-inventory wire shape returned by
  the plugin-details and marketplace-preview RPCs, built from a
  directory by :class:`PluginDirectoryScanner`; error payloads come
  from :meth:`PluginContents.error_result`.
* :class:`MarketplaceRefreshFailure` /
  :class:`MarketplaceRefreshResult` — typed result of a bulk
  refresh, letting the orchestration and message-rendering halves
  test independently. :meth:`MarketplaceRefreshResult.to_markdown`
  renders the shape to the ``## Marketplace refresh`` block the
  slash command emits.
* :class:`ArgsParseError` — sentinel arg-parse failure returned by
  every ``*Args.parse(rest)`` classmethod so the caller can branch
  on ``isinstance(...)`` without try/except.
* Slash-command arg schemas — :class:`InstallArgs`,
  :class:`UpdateArgs`, :class:`RemoveArgs`, :class:`MarketplaceAddArgs`,
  :class:`MarketplaceRemoveArgs`, :class:`MarketplaceRefreshArgs`,
  :class:`PluginsToggleArgs`. Each owns its ``parse(rest: list[str])``
  entrypoint so the verb bodies stop unpacking raw tuples.
* Gateway result models — :class:`InstallResult`,
  :class:`UpdateResult`, :class:`RemoveResult`,
  :class:`AddMarketplaceResult`, :class:`RefreshOneResult`,
  :class:`ResolvedInstallRef`. Each carries either a success
  payload or an error message; verbs match on ``.ok`` and render
  the CommandResult without touching installer/marketplace call
  exceptions directly.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

# ── Preview cache key + display source ──────────────────────────────


class PluginPreviewKey(BaseModel):
    """Frozen cache key for :attr:`PluginController._preview_cache`.
    Hashable so it can be used as a dict key.
    """

    model_config = ConfigDict(frozen=True)

    clone_url: str
    branch: str = ""
    subdir: str = ""


class PluginPreviewSource(BaseModel):
    """Where a previewed plugin comes from: a git repository and,
    optionally, a folder inside it.

    Displayed as ``"<url> [<subdir>]"``.
    """

    url: str = Field(description="The plugin's git repository URL.")
    subdir: str = Field(
        default="",
        description="Folder inside the repository that holds the plugin; empty for the root.",
    )

    # Display format: ``"<url> [<subdir>]"`` — the space between URL
    # and bracket is required. ``ClassVar`` so pydantic doesn't
    # interpret this as a model field.
    _DISPLAY_RE: ClassVar[re.Pattern[str]] = re.compile(r"^(.+?)\s+\[(.+?)\]\s*$")

    @classmethod
    def parse(cls, raw: str) -> PluginPreviewSource:
        """Split a user-facing ``"<url> [<subdir>]"`` string back into
        ``(url, subdir)``. Bare URLs (no bracket suffix) yield an
        empty ``subdir``."""
        stripped = raw.strip()
        m = cls._DISPLAY_RE.match(stripped)
        if m:
            return cls(url=m.group(1).strip(), subdir=m.group(2).strip())
        return cls(url=stripped, subdir="")

    def display(self) -> str:
        """Format back to the ``"<url> [<subdir>]"`` display string.
        Bare URLs come back as just the URL."""
        return f"{self.url} [{self.subdir}]" if self.subdir else self.url


# ── Plugin contents inventory ───────────────────────────────────────


class PluginSkillInfo(BaseModel):
    """A skill the plugin provides."""

    name: str = Field(description="The skill's name.")
    description: str = Field(default="", description="What the skill does; may be empty.")


class PluginAgentInfo(BaseModel):
    """An agent the plugin provides."""

    name: str = Field(description="The agent's name.")
    description: str = Field(default="", description="What the agent does; may be empty.")


class PluginHookInfo(BaseModel):
    """Hooks the plugin registers for one event."""

    event: str = Field(description="The hook event, e.g. `PreToolUse` or `Stop`.")
    count: int = Field(description="Number of hook handlers the plugin registers for the event.")


class PluginMCPServerInfo(BaseModel):
    """An MCP server the plugin provides."""

    name: str = Field(description="The server's name.")
    transport: str = Field(description="How igni talks to the server, e.g. `stdio` or `sse`.")
    command: str = Field(
        description="The command that starts the server (or its URL); may be empty."
    )


class PluginToolInfo(BaseModel):
    """A custom tool the plugin provides."""

    name: str = Field(description="The tool's name.")


class PluginContents(BaseModel):
    """What a plugin contains: its skills, agents, hooks, MCP servers,
    tools and README.

    Describes an installed plugin, or one previewed from a marketplace
    before installing. When ``error`` is set, the lists are empty."""

    # Built by PluginDirectoryScanner.scan, and by PluginController.preview
    # for marketplace previews (which unwraps a clone into the same shape).
    name: str = Field(default="", description="The plugin's name.")
    root_path: str = Field(default="", description="Folder the plugin was read from.")
    skills: list[PluginSkillInfo] = Field(default=[], description="Skills the plugin provides.")
    agents: list[PluginAgentInfo] = Field(default=[], description="Agents the plugin provides.")
    hooks: list[PluginHookInfo] = Field(
        default=[], description="Hooks the plugin registers, per event."
    )
    mcp_servers: list[PluginMCPServerInfo] = Field(
        default=[], description="MCP servers the plugin provides."
    )
    tools: list[PluginToolInfo] = Field(default=[], description="Custom tools the plugin provides.")
    readme: str = Field(
        default="", description="The plugin's README (Markdown); empty if it has none."
    )
    error: str = Field(
        default="",
        description=(
            "Why the plugin could not be read (not found, git clone failed, ...); empty on success."
        ),
    )
    preview_source: PluginPreviewSource | None = Field(
        default=None,
        description="Where a previewed plugin comes from; `null` for an installed plugin.",
    )

    @classmethod
    def error_result(cls, message: str) -> PluginContents:
        """Construct an error-only :class:`PluginContents` — used
        at every error branch of the preview path."""
        return cls(error=message)


# ── Marketplace refresh result ──────────────────────────────────────


class MarketplaceRefreshFailure(BaseModel):
    """One marketplace that failed during a bulk refresh."""

    name: str
    reason: str


class MarketplaceRefreshResult(BaseModel):
    """Typed result of a bulk marketplace refresh
    (:class:`~igni_core.backend.cmd_plugin.bulk_refresh.BulkRefreshRunner`)
    — keeps orchestration and message rendering independently testable.
    """

    ok: list[str] = Field(default_factory=list)
    failed: list[MarketplaceRefreshFailure] = Field(default_factory=list)

    def is_empty(self) -> bool:
        """True when neither success nor failure entries exist — the
        registry had no marketplaces to refresh."""
        return not self.ok and not self.failed

    def summary_line(self) -> str:
        """One-line outcome for the marketplace panel: counts of ok and
        failed refreshes, each failure named with its reason."""
        if self.is_empty():
            return "No marketplaces to refresh."
        line = f"Refreshed {len(self.ok)} ok"
        if self.failed:
            failures = ", ".join(f"{f.name} ({f.reason})" for f in self.failed)
            line += f"; {len(self.failed)} failed: {failures}"
        return line

    def to_markdown(self) -> str:
        """Render the bulk-refresh outcome as the markdown surface the
        slash command emits: ``## Marketplace refresh`` header, one
        bullet per marketplace, ``ok`` on success and
        ``failed (<reason>)`` on error. Pure function of ``self`` —
        no I/O, no external state."""
        lines = ["## Marketplace refresh"]
        for name in self.ok:
            lines.append(f"- {name}: ok")
        for failure in self.failed:
            lines.append(f"- {failure.name}: failed ({failure.reason})")
        return "\n".join(lines)


# ── Slash-command arg parse errors ──────────────────────────────────


class ArgsParseError(BaseModel):
    """Sentinel returned by every ``*Args.parse(rest)`` classmethod
    when the raw arg tokens don't match the schema — carries the
    user-facing usage message the verb surfaces via
    :meth:`CommandResult.error`. Not raised; returned so the caller
    branches on ``isinstance`` and doesn't need try/except."""

    message: str


# ── Slash-command arg schemas ──────────────────────────────────────


def _extract_ref_flag(rest: list[str]) -> tuple[str | None, list[str]]:
    """Pull ``--ref <value>`` out of ``rest``; return
    ``(ref | None, remaining_positionals)``. Shared between the
    install / update parsers. Kept private — verbs go through the
    ``*Args.parse`` classmethods, never this raw helper."""
    ref: str | None = None
    positional: list[str] = []
    i = 0
    while i < len(rest):
        if rest[i] == "--ref" and i + 1 < len(rest):
            ref = rest[i + 1]
            i += 2
            continue
        positional.append(rest[i])
        i += 1
    return ref, positional


class InstallArgs(BaseModel):
    """Parsed args for ``/plugin install <target> [--ref <ref>]``."""

    target: str
    ref: str | None = None

    _USAGE: ClassVar[str] = "Usage: /plugin install <git-url|@marketplace/plugin> [--ref <ref>]"

    @classmethod
    def parse(cls, rest: list[str]) -> InstallArgs | ArgsParseError:
        ref, positional = _extract_ref_flag(rest)
        if len(positional) != 1:
            return ArgsParseError(message=cls._USAGE)
        return cls(target=positional[0], ref=ref)


class UpdateArgs(BaseModel):
    """Parsed args for ``/plugin update <name> [--ref <ref>]``."""

    name: str
    ref: str | None = None

    _USAGE: ClassVar[str] = "Usage: /plugin update <name> [--ref <ref>]"

    @classmethod
    def parse(cls, rest: list[str]) -> UpdateArgs | ArgsParseError:
        ref, positional = _extract_ref_flag(rest)
        if len(positional) != 1:
            return ArgsParseError(message=cls._USAGE)
        return cls(name=positional[0], ref=ref)


class RemoveArgs(BaseModel):
    """Parsed args for ``/plugin remove <name>``."""

    name: str

    _USAGE: ClassVar[str] = "Usage: /plugin remove <name>"

    @classmethod
    def parse(cls, rest: list[str]) -> RemoveArgs | ArgsParseError:
        # ``--ref`` on a remove is nonsensical; strip it silently so
        # the positional count stays honest but keep the same helper.
        _ref, positional = _extract_ref_flag(rest)
        if len(positional) != 1:
            return ArgsParseError(message=cls._USAGE)
        return cls(name=positional[0])


class MarketplaceAddArgs(BaseModel):
    """Parsed args for ``/plugin marketplace add <url>``."""

    url: str

    _USAGE: ClassVar[str] = "Usage: /plugin marketplace add <git-url>"

    @classmethod
    def parse(cls, rest: list[str]) -> MarketplaceAddArgs | ArgsParseError:
        if len(rest) != 1:
            return ArgsParseError(message=cls._USAGE)
        return cls(url=rest[0])


class MarketplaceRemoveArgs(BaseModel):
    """Parsed args for ``/plugin marketplace remove <name>``."""

    name: str

    _USAGE: ClassVar[str] = "Usage: /plugin marketplace remove <name>"

    @classmethod
    def parse(cls, rest: list[str]) -> MarketplaceRemoveArgs | ArgsParseError:
        if len(rest) != 1:
            return ArgsParseError(message=cls._USAGE)
        return cls(name=rest[0])


class MarketplaceRefreshArgs(BaseModel):
    """Parsed args for ``/plugin marketplace refresh [<name>]``. When
    ``name`` is empty, the verb refreshes every registered
    marketplace."""

    name: str = ""

    _USAGE: ClassVar[str] = "Usage: /plugin marketplace refresh [<name>]"

    @classmethod
    def parse(cls, rest: list[str]) -> MarketplaceRefreshArgs | ArgsParseError:
        if len(rest) > 1:
            return ArgsParseError(message=cls._USAGE)
        return cls(name=rest[0] if rest else "")


class PluginsToggleArgs(BaseModel):
    """Parsed args for ``/plugins {enable|disable} <name>``. The verb
    class carries the toggle mode itself (polymorphic subclasses),
    so the args model only holds the plugin name."""

    name: str

    @classmethod
    def parse(cls, subcommand: str, rest_raw: str) -> PluginsToggleArgs | ArgsParseError:
        name = rest_raw.strip()
        if not name:
            return ArgsParseError(message=f"Usage: /plugins {subcommand} <plugin-name>")
        return cls(name=name)


# ── Gateway result models ──────────────────────────────────────────


class ResolvedInstallRef(BaseModel):
    """Successful resolution of an ``@<marketplace>/<plugin>`` install
    ref. Carries the concrete git URL, optional subdirectory, and the
    marketplace-supplied default ref (branch/tag/SHA) — the verb
    layers it over the user's explicit ``--ref`` if provided."""

    url: str
    subdir: str | None = None
    ref: str | None = None


class InstallResult(BaseModel):
    """Outcome of :meth:`PluginBackendGateway.install`. On success:
    ``ok=True`` and ``name`` / ``version`` populated. On failure:
    ``ok=False`` and ``error`` carries the user-facing message
    (already prefixed with ``git error:`` where appropriate)."""

    ok: bool
    name: str = ""
    version: str = ""
    error: str = ""


class UpdateResult(BaseModel):
    """Outcome of :meth:`PluginBackendGateway.update`. ``sha`` is the
    new HEAD after the update (full 40-char SHA — verb truncates for
    display)."""

    ok: bool
    sha: str = ""
    error: str = ""


class RemoveResult(BaseModel):
    """Outcome of :meth:`PluginBackendGateway.remove`. Success carries
    no payload beyond the plugin having been removed."""

    ok: bool
    error: str = ""


class AddMarketplaceResult(BaseModel):
    """Outcome of :meth:`PluginBackendGateway.add_marketplace`. On
    success carries the registered entry's name and cached plugin
    count for the verb's confirmation message."""

    ok: bool
    name: str = ""
    plugin_count: int = 0
    error: str = ""


class RefreshOneResult(BaseModel):
    """Outcome of :meth:`PluginBackendGateway.refresh_marketplace` for
    a single named marketplace. ``not_found=True`` when the name
    isn't registered — separate from a git failure so the verb can
    render a distinct error message."""

    ok: bool
    name: str = ""
    plugin_count: int = 0
    not_found: bool = False
    error: str = ""


# The gateway holds a small dependency on ``MarketplaceRegistry`` from
# the core plugins module — declared here as ``Any`` in the schema
# surface (the concrete type flows through gateway internals and the
# bulk-refresh runner without needing a schema-level import).
_MarketplaceRegistry = Any
