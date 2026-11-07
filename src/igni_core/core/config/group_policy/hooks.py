"""A pack's hook entries, merged into the one settings-shaped file igni reads."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from igni_core.core.config.group_policy.entry import GroupPolicyEntry

logger = logging.getLogger(__name__)

#: What a group's hook writes to reach a script the same group ships.
#: Expanded here, because only this side knows where the pack landed.
GROUP_SCRIPTS_PLACEHOLDER = "{group_scripts}"


class HookDeclaration(BaseModel):
    """One hook declaration. Only ``command`` is read here; the rest is carried as sent."""

    model_config = ConfigDict(extra="allow")

    command: str | None = None

    @classmethod
    def read(cls, value: JsonValue) -> HookDeclaration | JsonValue:
        """A declaration, or ``value`` unchanged when it is not one this side can read."""
        if not isinstance(value, dict):
            return value
        try:
            return cls.model_validate(value)
        except ValidationError:
            return value

    def expanded(self, scripts_dir: Path) -> HookDeclaration:
        """Pointed at the scripts this pack materialised.

        Only this side knows the directory, and it is per-machine, so the
        server stores a placeholder. Left untouched when absent, so a command
        naming an absolute path or something baked into the image still works.
        """
        if self.command is None:
            return self
        command = self.command.replace(GROUP_SCRIPTS_PLACEHOLDER, str(scripts_dir))
        return self.model_copy(update={"command": command})

    def as_json(self) -> JsonValue:
        return self.model_dump(exclude_unset=True)


class HooksFile(BaseModel):
    """``hooks/settings.json`` — the shape :class:`HookLoader` reads."""

    hooks: dict[str, list[JsonValue]]


class HookSet(BaseModel):
    """The hook declarations of one pack, keyed by event."""

    events: dict[str, list[HookDeclaration | JsonValue]] = Field(default_factory=dict)

    def add(self, entry: GroupPolicyEntry) -> bool:
        """Fold one hook entry in; ``False`` (and a warning) when it says nothing usable.

        Hooks are the one kind that is not a file per entry: they are
        declarations keyed by event. An entry carries either a bare
        declaration naming its ``event`` or a whole ``{"hooks": {...}}`` block.
        """
        try:
            parsed = json.loads(entry.content)
        except (TypeError, ValueError):
            logger.warning("Group hook %r is not valid JSON; skipping", entry.entry_name)
            return False

        block = parsed.get("hooks") if isinstance(parsed, dict) else None
        if isinstance(block, dict):
            for event, declarations in block.items():
                listed = declarations if isinstance(declarations, list) else [declarations]
                self.events.setdefault(event, []).extend(HookDeclaration.read(d) for d in listed)
            return True

        if isinstance(parsed, dict) and parsed.get("event"):
            event = str(parsed.pop("event"))
            self.events.setdefault(event, []).append(HookDeclaration.read(parsed))
            return True

        logger.warning(
            "Group hook %r names no event and carries no hooks block; skipping",
            entry.entry_name,
        )
        return False

    def expanded(self, scripts_dir: Path) -> HookSet:
        """Every declaration's ``{group_scripts}`` resolved — once, on the merged result,
        so a bare declaration and one nested in a ``hooks`` block are both covered."""
        return HookSet(
            events={
                event: [
                    d.expanded(scripts_dir) if isinstance(d, HookDeclaration) else d
                    for d in declarations
                ]
                for event, declarations in self.events.items()
            }
        )

    def write_to(self, path: Path) -> None:
        """Write the merged file, or remove it when the pack has no hooks."""
        if not self.events:
            path.unlink(missing_ok=True)
            return
        document = HooksFile(
            hooks={
                event: [d.as_json() if isinstance(d, HookDeclaration) else d for d in declarations]
                for event, declarations in self.events.items()
            }
        )
        path.write_text(json.dumps(document.model_dump(), indent=2), encoding="utf-8")
