"""One entry of a group pack, and the rules its name has to follow."""

from __future__ import annotations

import json
import logging
import re

import yaml
from pydantic import BaseModel, ConfigDict, JsonValue, model_validator

logger = logging.getLogger(__name__)

# The split :class:`AgentMarkdownFile` uses, so a model written here parses
# back out of the file the loader reads. Not imported from there:
# ``core.agents`` imports the auth package, which imports this module.
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)


class EntryNameCheck(BaseModel):
    """Whether an entry name is a plain filename, and why not."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    name: str = ""
    reason: str = ""


class GroupPolicyEntry(BaseModel):
    """One entry from a group pack.

    ``source_url`` / ``source_ref`` / ``source_subdir`` are an optional
    trio that mirrors :class:`MarketplacePluginEntry.resolved_source`.
    When ``kind == "plugins"`` and ``source_url`` is set, the policy
    cache treats the entry as "install this plugin from this git URL"
    (via :class:`PluginInstaller`) rather than "write this YAML file
    to disk". The cache also takes them on other kinds for forward
    compatibility.

    There is no ``extensions`` kind. An extension reaches a machine
    from its service through igni server (its ``docs/service-identity.md``
    §8), never from a group's policy; an entry of that kind is
    ignored with a warning.
    """

    kind: str  # agents | mcps | plugins | settings | hooks | …
    entry_name: str
    content: str
    content_type: str  # markdown | yaml | json
    enabled: bool = True

    # Agents only: the model (usually a LoRA adapter) this one runs
    # against. Written into the materialised file's frontmatter, where
    # the loader already reads it. Null inherits the session default.
    model: str | None = None

    # Plugin install source — same shape as marketplace. All three are
    # optional; if any is set, ``source_url`` must be too (half-filled
    # installs would fail in ``PluginInstaller.install`` anyway).
    source_url: str | None = None
    source_ref: str | None = None
    source_subdir: str | None = None

    @model_validator(mode="after")
    def _source_fields_are_complete(self) -> GroupPolicyEntry:
        """Reject half-filled plugin source specs.

        The ``git-subdir`` flow needs the parent URL; ``ref`` and
        ``subdir`` are meaningless without it. Empty strings are
        treated as absent — wire formats commonly serialize "no
        value" as ``""``, and ``is None`` alone would let an
        ``""`` + ``"main"`` slip past the guard.
        """
        url_present = bool(self.source_url and self.source_url.strip())
        ref_present = bool(self.source_ref and self.source_ref.strip())
        subdir_present = bool(self.source_subdir and self.source_subdir.strip())

        if (ref_present or subdir_present) and not url_present:
            raise ValueError("source_ref / source_subdir require source_url to be set")
        return self

    @staticmethod
    def check_name(entry_name: str) -> EntryNameCheck:
        """Whether *entry_name* is a plain filename.

        The server sends these names and the cache turns them into paths.
        ``Path`` happily accepts a separator, so an entry named
        ``../../../../../../tmp/x`` wrote ``/tmp/x.json`` — outside the
        cache, on every machine that synced the pack. For ``scripts`` the
        file is written mode 0700, which makes it an executable dropped
        anywhere the user can write.

        Checked here rather than only on the server because this is the
        side that must not be talked into it: a client can be pointed at a
        server somebody else runs. The server validates it too.

        Rejects rather than sanitises. A name that has to be rewritten to be
        safe is not the name the admin thinks they published.
        """
        name = (entry_name or "").strip()
        if not name:
            return EntryNameCheck(ok=False, reason="entry name is empty")
        if name in {".", ".."}:
            reason = f"entry name {entry_name!r} is a directory reference"
        elif "/" in name or "\\" in name:
            reason = f"entry name {entry_name!r} contains a path separator"
        elif "\x00" in name:
            reason = f"entry name {entry_name!r} contains a NUL byte"
        elif name.startswith("~"):
            reason = f"entry name {entry_name!r} starts with a home reference"
        # ``Path('C:x')`` is drive-relative on Windows, and a bare drive
        # letter is not a filename anywhere.
        elif len(name) >= 2 and name[1] == ":":
            reason = f"entry name {entry_name!r} looks like a drive path"
        else:
            return EntryNameCheck(ok=True, name=name)
        return EntryNameCheck(ok=False, reason=reason)

    def safe_name(self) -> EntryNameCheck:
        """This entry's name, checked — see :meth:`check_name`."""
        return self.check_name(self.entry_name)

    def rendered_content(self) -> str:
        """The content with ``model:`` set in its frontmatter.

        An agent's model is already a frontmatter key the loader honours
        (:attr:`AgentDefinition.model` → :meth:`AgentBuilder._resolve_model`),
        so the pack's per-agent model needs no new plumbing — it just has to
        reach the file the loader reads.

        Content that cannot be parsed is returned untouched: it would fail to
        load as an agent anyway, and the loader reports that with the file name.
        """
        if not self.model:
            return self.content
        match = _FRONTMATTER_RE.match(self.content)
        if not match:
            logger.warning("Group agent has no YAML frontmatter; cannot set model %r", self.model)
            return self.content
        try:
            frontmatter = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            logger.warning(
                "Group agent has unreadable frontmatter; cannot set model %r", self.model
            )
            return self.content
        if not isinstance(frontmatter, dict):
            return self.content
        frontmatter["model"] = self.model
        rendered = yaml.safe_dump(frontmatter, sort_keys=False).strip()
        return f"---\n{rendered}\n---\n\n{match.group(2).strip()}\n"

    def mcp_server_config(self) -> JsonValue:
        """The content as one MCP server's JSON, or the raw text when it is not JSON.

        The BE serialises ``mcps`` entries as raw MCP server JSON (matching
        :class:`MCPServerConfig` fields); some legacy entries store a string.
        Both shapes are accepted at load time.
        """
        try:
            return json.loads(self.content)
        except (TypeError, ValueError):
            return self.content
