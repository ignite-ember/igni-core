"""``host.models``: the model registry and the model in use.

The registry is igni's own, and one row of it carries the credential
the model is reached with. That is not published: an extension is told
*where* the key comes from — the environment variable's name — and
never the key itself, nor the command that would print it.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from igni_core.backend.schemas_rpc import ModelRegistryResult
from igni_core.core.config.model_entry import ModelRegistryEntry
from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.members.base import Members
from igni_core.extensions.host.members.call import Call
from igni_core.extensions.host.members.reads import read_answer
from igni_core.extensions.host.members.tag import get, put
from igni_core.extensions.host.rpc_wire.settings import SwitchModelArgs
from igni_core.extensions.host.spec import ErrorCode
from igni_core.protocol.rpc import RpcMethod

#: The registry row's fields that carry a credential, and are not published.
WITHHELD = frozenset({"api_key", "api_key_cmd"})


class ModelEntry(BaseModel):
    """One model in the registry, as an extension may see it."""

    model_config = ConfigDict(extra="ignore")

    model_id: str
    provider: str
    url: str | None
    #: The environment variable the key is read from — its name, never
    #: the key.
    api_key_env: str | None
    temperature: float | None
    max_tokens: int | None
    timeout: int
    vision: bool
    context_window: int | None
    thinking: dict[str, Any] | None
    effort: str | None
    prompt_caching: bool

    @classmethod
    def of(cls, entry: ModelRegistryEntry) -> ModelEntry:
        """The registry's row, without the credential it carries."""
        return cls(
            model_id=entry.model_id,
            provider=entry.provider,
            url=entry.url,
            api_key_env=entry.api_key_env,
            temperature=entry.temperature,
            max_tokens=entry.max_tokens,
            timeout=entry.timeout,
            vision=entry.vision,
            context_window=entry.context_window,
            thinking=entry.thinking,
            effort=entry.effort,
            prompt_caching=entry.prompt_caching,
        )


class ModelRegistry(BaseModel):
    """Every model igni knows: the default, the ceiling, and the rows."""

    model_config = ConfigDict(extra="ignore")

    default: str
    max_context_window: int
    registry: dict[str, ModelEntry]

    @classmethod
    def of(cls, found: ModelRegistryResult) -> ModelRegistry:
        """The registry igni answered with, without the credentials in it."""
        return cls(
            default=found.default,
            max_context_window=found.max_context_window,
            registry={name: ModelEntry.of(entry) for name, entry in found.registry.items()},
        )


class ModelMembers(Members):
    @get("models.registry")
    async def registry(self, c: Call) -> ModelRegistry:
        found = read_answer(
            ModelRegistryResult,
            await self.rpc(RpcMethod.GET_MODEL_REGISTRY, c=c),
            method="models.registry",
        )
        return ModelRegistry.of(found)

    @get("models.current")
    async def current(self, c: Call) -> str:
        return (await self.read_status(c)).model

    @put("models.current")
    async def set_current(self, c: Call) -> None:
        name = str(c.value or "")
        found = read_answer(
            ModelRegistryResult,
            await self.rpc(RpcMethod.GET_MODEL_REGISTRY, c=c),
            method="models.registry",
        )
        if name not in found.registry:
            raise HostModelError(ErrorCode.REFUSED, f"no model {name!r} is configured")
        await self.rpc_checked(RpcMethod.SWITCH_MODEL, SwitchModelArgs(model_name=name), c=c)


__all__ = ["WITHHELD", "ModelEntry", "ModelMembers", "ModelRegistry"]
