"""``ModelsConfig`` — the ``models`` block of ``Settings``.

Every registry row is a typed :class:`ModelRegistryEntry`: YAML rows
are validated into it when the block loads, and cloud discovery writes
entries directly. The queries below read the registry; they never
coerce.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from igni_core.core.config.model_entry import CLOUD_TOKEN_SENTINEL, ModelRegistryEntry


class ModelsConfig(BaseModel):
    # Empty means "auto" — the resolver falls back to the first key
    # in ``registry`` at lookup time. Cloud discovery sets this at
    # session start once the cloud catalogue is merged in. Users
    # explicitly pinning a model via ``/model`` or config override
    # set it directly.
    default: str = ""
    max_context_window: int = 200_000
    max_run_timeout: int = 300  # total timeout for a single arun() call (seconds)
    # Retry count for transient model-API failures (timeouts, 5xx, a
    # dropped stream), per model call: set on every model ModelRegistry
    # builds. A reply cut at its length limit or stopped by the provider
    # is not retried (B31). Surfaced here so users can tune it from
    # settings without touching code.
    retries: int = 2
    registry: dict[str, ModelRegistryEntry] = Field(default_factory=dict)

    def entry(self, name: str) -> ModelRegistryEntry | None:
        """Registry row ``name``, or ``None`` when there is no such row."""
        return self.registry.get(name)

    def has_vision(self, name: str) -> bool:
        """True when ``name`` is a registry entry flagged ``vision``.
        Unknown names are non-vision."""
        entry = self.entry(name)
        return entry is not None and entry.vision

    def current_uses_cloud_token(self) -> bool:
        """True when :attr:`default` resolves to a registry entry
        whose ``api_key`` is the cloud sentinel. False when the
        default is unset, missing from the registry, or configured
        with an inline / env-backed api_key."""
        entry = self.entry(self.default)
        return entry is not None and entry.api_key == CLOUD_TOKEN_SENTINEL

    def find_non_cloud_fallback(self) -> str | None:
        """Return the first registry name whose entry has a non-cloud
        ``api_key`` (any non-empty string that isn't
        :data:`CLOUD_TOKEN_SENTINEL`), or ``None`` when the registry
        has no such entry.

        Used by ``/logout`` to keep the session usable after cloud
        credentials are dropped — we don't want the default model
        to remain cloud-backed with no token behind it."""
        for name, entry in self.registry.items():
            if entry.api_key and entry.api_key != CLOUD_TOKEN_SENTINEL:
                return name
        return None
