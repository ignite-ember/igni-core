"""The YAML header of a markdown-authored command.

A command file may open with a ``---`` block of YAML frontmatter. The
block is user-authored, so the parser is deliberately permissive:
anything YAML accepts is accepted, unknown keys are ignored, and a
block that cannot be parsed costs the command its header rather than
the whole file.

The four keys igni reads are modelled here, so nothing downstream of
this module ever sees a raw mapping.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

logger = logging.getLogger(__name__)

#: The header block: ``---`` on a line of its own, the YAML, ``---``
#: again. Everything after the closing fence is the body.
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)


class CommandFrontmatter(BaseModel):
    """The header of a markdown command file.

    Every field is optional — a file with no header at all is a
    command with no description, no allowed tools and no preferred
    model. The field names are igni's; the YAML keys they are written
    under are Claude Code's, and :meth:`from_mapping` is where the
    two meet.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    description: str = ""
    allowed_tools: tuple[str, ...] = ()
    argument_hint: str = ""
    model: str | None = None

    @field_validator("description", "argument_hint", mode="before")
    @classmethod
    def _as_text(cls, raw: Any) -> str:
        """A header value that is not a string is read as one.

        ``description: 2024`` is a person's year, not a number they
        want to count; the header is prose, so it is read as prose.
        An explicitly empty value is empty, not the word "None".
        """
        if raw is None:
            return ""
        return str(raw).strip()

    @field_validator("model", mode="before")
    @classmethod
    def _as_model_name(cls, raw: Any) -> str | None:
        """The preferred model, or none.

        An empty value means "no preference" — the session's ``/model``
        decides — so it is ``None`` rather than an empty string.
        """
        if not raw:
            return None
        return str(raw).strip()

    @field_validator("allowed_tools", mode="before")
    @classmethod
    def _as_tool_names(cls, raw: Any) -> tuple[str, ...]:
        """Normalize the ``allowed-tools`` value into a tuple.

        Accepts either the CC comma-separated string form
        (``"Read, Bash"``) or a native YAML list (``["Read", "Bash"]``).
        Anything else collapses to an empty tuple so an authorship
        mistake fails closed rather than granting permissions nobody
        asked for.
        """
        if isinstance(raw, str):
            return tuple(t.strip() for t in raw.split(",") if t.strip())
        if isinstance(raw, list):
            return tuple(str(t).strip() for t in raw if str(t).strip())
        return ()

    @classmethod
    def parse(cls, content: str) -> tuple[CommandFrontmatter, str]:
        """Split the header off ``content``.

        Returns the header and the body. A file with no header, one
        whose header does not parse, and one whose header is not a
        mapping all give an empty header — the body survives in every
        case, because a broken header is not a broken command.
        """
        match = _FRONTMATTER_RE.match(content)
        if not match:
            return cls(), content
        raw, body = match.group(1), match.group(2)
        try:
            meta = yaml.safe_load(raw) or {}
        except yaml.YAMLError as exc:
            logger.debug("Markdown command frontmatter parse failed: %s", exc)
            return cls(), body
        if not isinstance(meta, dict):
            return cls(), body
        return cls.from_mapping(meta), body

    @classmethod
    def from_mapping(cls, meta: dict[str, Any]) -> CommandFrontmatter:
        """Build a header from an already-parsed mapping.

        The keys are Claude Code's (``allowed-tools``, ``argument-hint``)
        rather than igni's field names, so the mapping is read through
        the aliases. A value that does not fit its field falls back to
        that field's default rather than sinking the whole header:
        a command with a broken description is still a command.
        """
        return cls.model_validate(
            {
                "description": meta.get("description", ""),
                "allowed_tools": meta.get("allowed-tools", ""),
                "argument_hint": meta.get("argument-hint", ""),
                "model": meta.get("model"),
            }
        )


__all__ = ["CommandFrontmatter"]
