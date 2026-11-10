"""Wire schemas for the panel RPCs.

Companion module to :mod:`server_panels` — follows the sibling
``schemas_*.py`` convention (see :mod:`schemas_run`,
:mod:`schemas_hitl`, etc.) so every RPC response served by the
panel controllers is a typed Pydantic model rather than a raw
dict.

Contains:

* :class:`OutputStyleInfo` / :class:`OutputStylesResult` — moved
  verbatim from :mod:`server_panels` so the controller file drops
  to a thin composition facade.
* :class:`HookEntryView` — typed replacement for the ``list[dict]``
  the hooks panel used to return. Its :meth:`HookEntryView.from_hook`
  factory concentrates the (heterogeneous) hook-object reach-ins
  in one place.
* :class:`BuiltinSlashCommand` / :class:`MarkdownSlashCommand` /
  :class:`SkillSlashCommand` + the discriminated
  :data:`SlashCommandEntry` alias — typed replacement for the
  ``list[dict]`` the slash-command catalog used to return, one
  variant per source with dedicated ``from_*`` factories.
* :class:`PromoteEphemeralResult` / :class:`DiscardEphemeralResult`
  — typed results for the promote/discard mutators, replacing the
  overloaded ``msg.Info`` (which conflates success and failure).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, Field

from igni_core.core.contributed_commands import ContributedCommand
from igni_core.protocol.redaction import url_without_credentials

if TYPE_CHECKING:
    from igni_core.core.hooks.schemas import HookDefinition
    from igni_core.core.skills.parser import SkillEntry
    from igni_core.core.utils.markdown_commands import MarkdownCommand


class OutputStyleInfo(BaseModel):
    """One output style the agent can answer in."""

    name: str = Field(description="The style's name, as it is chosen.")
    description: str = Field(description="What the style does, in one line.")


class OutputStylesResult(BaseModel):
    """The output styles available, and which one is in use."""

    active: str = Field(description="The name of the style in use.")
    styles: list[OutputStyleInfo] = Field(description="Every style that can be chosen.")


class HookEntryView(BaseModel):
    """One configured hook: what it runs, and on which event.

    Hooks come in several types (a shell command, an HTTP call, an MCP
    tool call, …). A field that does not apply to a hook's type is
    empty, e.g. ``command`` on an HTTP hook or ``url`` on a command hook.
    """

    event: str = Field(description="The event the hook runs on.")
    type: str = Field(default="", description="The hook's type, e.g. `command` or `http`.")
    command: str = Field(default="", description="The shell command a `command` hook runs.")
    url: str = Field(
        default="", description="The URL an `http` hook calls, with any credentials removed."
    )
    matcher: str = Field(
        default="", description="Which targets the hook applies to; empty matches every target."
    )
    timeout_ms: int = Field(default=0, description="How long the hook may run, in milliseconds.")
    background: bool = Field(
        default=False, description="Whether the hook runs without making the agent wait for it."
    )
    header_names: list[str] = Field(
        default_factory=list,
        description="The names of the headers an `http` hook sends. Never their values, "
        "which may be credentials.",
    )

    @classmethod
    def from_hook(cls, hook: HookDefinition, event: object) -> HookEntryView:
        """Project a :class:`HookDefinition` into the panel view.

        Uses ``getattr`` defensively — the panel is used against
        older sessions whose hook objects predate newer optional
        fields (``background``, structured ``headers``). A header's value
        and a URL's credentials stay out of it.
        """
        return cls(
            event=str(event),
            type=getattr(hook, "type", "") or "",
            command=getattr(hook, "command", "") or "",
            url=url_without_credentials(getattr(hook, "url", "") or ""),
            matcher=getattr(hook, "matcher", "") or "",
            timeout_ms=int(getattr(hook, "timeout", 0) or 0),
            background=bool(getattr(hook, "background", False)),
            header_names=sorted(getattr(hook, "headers", {}) or {}),
        )


class BuiltinSlashCommand(BaseModel):
    """A slash command built into igni."""

    source: Literal["builtin"] = Field(
        default="builtin", description="Always `builtin`: a command built into igni."
    )
    name: str = Field(description="The command's name, without the leading `/`.")
    description: str = Field(default="", description="What the command does, in one line.")
    argument_hint: str = Field(
        default="", description="A hint for the arguments it takes; empty when it takes none."
    )

    @classmethod
    def from_builtin(cls, name: str, description: str) -> BuiltinSlashCommand:
        return cls(name=name, description=description)


class MarkdownSlashCommand(BaseModel):
    """A slash command the user wrote as a Markdown file."""

    source: Literal["markdown"] = Field(
        default="markdown", description="Always `markdown`: a command defined in a Markdown file."
    )
    name: str = Field(description="The command's name, without the leading `/`.")
    description: str = Field(default="", description="What the command does, in one line.")
    argument_hint: str = Field(
        default="", description="A hint for the arguments it takes; empty when it takes none."
    )

    @classmethod
    def from_markdown(cls, md: MarkdownCommand) -> MarkdownSlashCommand:
        return cls(
            name=md.name,
            description=md.description,
            argument_hint=md.argument_hint,
        )


class SkillSlashCommand(BaseModel):
    """A skill the user can run as a slash command."""

    source: Literal["skill"] = Field(default="skill", description="Always `skill`: a skill.")
    name: str = Field(description="The skill's name, without the leading `/`.")
    description: str = Field(default="", description="What the skill does, in one line.")
    argument_hint: str = Field(
        default="", description="A hint for the arguments it takes; empty when it takes none."
    )

    @classmethod
    def from_skill(cls, skill: SkillEntry) -> SkillSlashCommand:
        return cls(
            name=skill.name,
            description=skill.description,
            argument_hint=getattr(skill, "argument_hint", ""),
        )


class ExtensionSlashCommand(BaseModel):
    """A slash command an installed extension answers."""

    # Carries `owner` because a command that fails should be attributable:
    # "/compliance did not answer" is a support ticket, and "acme.scanner
    # did not answer /compliance" is a thing an administrator can act on.
    source: Literal["extension"] = Field(
        default="extension", description="Always `extension`: a command an extension answers."
    )
    name: str = Field(description="The command's name, without the leading `/`.")
    description: str = Field(default="", description="What the command does, in one line.")
    argument_hint: str = Field(
        default="", description="A hint for the arguments it takes; empty when it takes none."
    )
    owner: str = Field(default="", description="The id of the extension that answers it.")

    @classmethod
    def from_contributed(cls, command: ContributedCommand) -> ExtensionSlashCommand:
        return cls(
            name=command.name,
            description=command.description,
            owner=command.owner,
        )


SlashCommandEntry = Annotated[
    BuiltinSlashCommand | MarkdownSlashCommand | SkillSlashCommand | ExtensionSlashCommand,
    Field(discriminator="source"),
]
"""Discriminated union — the ``source`` string picks the variant.

Kept as a bare-string discriminator
(``'builtin'|'markdown'|'skill'|'extension'``) rather than an Enum so
IDE-plugin JSON consumers see plain string literals in the wire
payload.
"""


class PromoteEphemeralResult(BaseModel):
    """The outcome of saving an agent created during a session as a
    permanent agent file."""

    ok: bool = Field(description="Whether the agent was saved.")
    name: str = Field(description="The agent's name.")
    dest: str = Field(default="", description="The file it was saved to; empty when `ok` is false.")
    reason: str = Field(default="", description="Why it was not saved; empty when `ok` is true.")


class DiscardEphemeralResult(BaseModel):
    """The outcome of discarding an agent created during a session."""

    ok: bool = Field(description="Whether the agent was discarded.")
    name: str = Field(description="The agent's name.")
    reason: str = Field(
        default="", description="Why it was not discarded; empty when `ok` is true."
    )


__all__ = [
    "OutputStyleInfo",
    "OutputStylesResult",
    "HookEntryView",
    "BuiltinSlashCommand",
    "MarkdownSlashCommand",
    "SkillSlashCommand",
    "SlashCommandEntry",
    "PromoteEphemeralResult",
    "DiscardEphemeralResult",
]
