"""Typed view of a Learning Machine recall, rendered for ``/memory``.

``LearningMachine.arecall`` returns ``{store_name: store_result}``.
Each known store maps onto a :class:`KnownStoreSection` subclass
that reads the store result by attribute (``from_attributes``) and
renders its own markdown. Stores this module does not know about go
through :class:`FallbackSection`. Fetching the recall is the caller's
job; this module only shapes and renders it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from igni_core.backend.command_result import CommandResult


class MemoryEntry(BaseModel):
    """One ``{"content": ...}`` row of the ``user_memory`` store."""

    content: str = ""


class EntityEntry(BaseModel):
    """One entity of the ``entity_memory`` store's directory."""

    model_config = ConfigDict(from_attributes=True)

    name: str | None = None
    description: str | None = None

    def to_line(self) -> str:
        return f"- **{self.name or '?'}**: {self.description or ''}\n"


class LearningStoreSection(BaseModel):
    """One store's markdown section; empty bodies render to ""."""

    TITLE: ClassVar[str] = ""

    def heading(self) -> str:
        return self.TITLE

    def to_markdown(self) -> str:
        return ""

    def render(self) -> str:
        body = self.to_markdown()
        if not body.strip():
            return ""
        return f"## {self.heading()}\n{body}"


class KnownStoreSection(LearningStoreSection):
    """A section for a store this module knows by name."""

    STORE_NAME: ClassVar[str] = ""

    @classmethod
    def from_store(cls, store_data: object) -> KnownStoreSection:
        return cls.model_validate(store_data, from_attributes=True)


class UserProfileSection(KnownStoreSection):
    """``user_profile`` — one bullet per set attribute, in field order.

    ``role`` / ``expertise`` / ``preferences`` are read when a custom
    profile schema carries them.
    """

    STORE_NAME: ClassVar[str] = "user_profile"
    TITLE: ClassVar[str] = "User Profile"

    name: str | None = None
    preferred_name: str | None = None
    role: str | None = None
    expertise: str | None = None
    preferences: str | None = None

    def to_markdown(self) -> str:
        return "".join(
            f"- **{attr.replace('_', ' ').title()}**: {val}\n"
            for attr, val in self.model_dump().items()
            if val
        )


class UserMemorySection(KnownStoreSection):
    """``user_memory`` — one bullet per memory with content."""

    STORE_NAME: ClassVar[str] = "user_memory"
    TITLE: ClassVar[str] = "User Memory"

    memories: list[MemoryEntry] = Field(default_factory=list)

    def to_markdown(self) -> str:
        return "".join(f"- {m.content}\n" for m in self.memories if m.content)


class EntityMemorySection(KnownStoreSection):
    """``entity_memory`` — one bullet per entity in the directory.

    The store's recall is a mapping (``directory`` / ``entities`` /
    ``related_names``); ``entities`` is only filled for a message or
    entity lookup, so ``/memory`` lists the directory.
    """

    STORE_NAME: ClassVar[str] = "entity_memory"
    TITLE: ClassVar[str] = "Entity Memory"

    directory: list[EntityEntry] = Field(default_factory=list)

    def to_markdown(self) -> str:
        return "".join(e.to_line() for e in self.directory)


class SessionContextSection(KnownStoreSection):
    """``session_context`` — the summary, if any."""

    STORE_NAME: ClassVar[str] = "session_context"
    TITLE: ClassVar[str] = "Session Context"

    summary: str | None = None

    def to_markdown(self) -> str:
        return f"{self.summary}\n" if self.summary else ""


class FallbackSection(LearningStoreSection):
    """A store this module does not know, rendered as its ``str``."""

    store_name: str
    payload: str

    @classmethod
    def from_store(cls, store_name: str, store_data: object) -> FallbackSection:
        return cls(store_name=store_name, payload=f"{store_data}\n")

    def heading(self) -> str:
        return self.store_name.replace("_", " ").title()

    def to_markdown(self) -> str:
        return self.payload


class LearningRecall(BaseModel):
    """One recall, as the sections ``/memory`` renders."""

    sections: list[LearningStoreSection] = Field(default_factory=list)

    KNOWN_SECTIONS: ClassVar[tuple[type[KnownStoreSection], ...]] = (
        UserProfileSection,
        UserMemorySection,
        EntityMemorySection,
        SessionContextSection,
    )
    EMPTY_MESSAGE: ClassVar[str] = (
        "No learnings stored yet. The agent learns from your conversations automatically."
    )

    @classmethod
    def section_for(cls, store_name: str, store_data: object) -> LearningStoreSection:
        for section_type in cls.KNOWN_SECTIONS:
            if store_name == section_type.STORE_NAME:
                return section_type.from_store(store_data)
        return FallbackSection.from_store(store_name, store_data)

    @classmethod
    def from_stores(cls, stores: Mapping[str, object]) -> LearningRecall:
        return cls(sections=[cls.section_for(name, data) for name, data in stores.items() if data])

    def to_command_result(self) -> CommandResult:
        rendered = [r for r in (s.render() for s in self.sections) if r]
        if not rendered:
            return CommandResult.info(self.EMPTY_MESSAGE)
        return CommandResult.markdown("\n\n".join(rendered))


__all__ = [
    "EntityEntry",
    "EntityMemorySection",
    "FallbackSection",
    "KnownStoreSection",
    "LearningRecall",
    "LearningStoreSection",
    "MemoryEntry",
    "SessionContextSection",
    "UserMemorySection",
    "UserProfileSection",
]
