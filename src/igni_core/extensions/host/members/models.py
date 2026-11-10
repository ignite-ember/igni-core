"""``host.models``: the model registry and the model in use.

The registry is igni's own, and one row of it carries the credential
the model is reached with. That is not published: igni answers the
registry with a row that says *whether* the entry has a key, never the
key, never the environment variable it is read from, and never the
command that would print it. An extension sees the same row igni's
clients see.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from igni_core.backend.schemas_rpc import ModelRegistryEntryView, ModelRegistryResult
from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.members.base import Members
from igni_core.extensions.host.members.call import Call
from igni_core.extensions.host.members.reads import read_answer
from igni_core.extensions.host.members.tag import get, put
from igni_core.extensions.host.rpc_wire.settings import SwitchModelArgs
from igni_core.extensions.host.spec import ErrorCode
from igni_core.protocol.json_value import WireJson
from igni_core.protocol.rpc import RpcMethod

#: The configuration row's fields that carry a credential. None of them
#: reaches the wire, so none of them reaches an extension either.
WITHHELD = frozenset({"api_key", "api_key_cmd"})


class ModelEntry(BaseModel):
    """One model in the registry, as an extension may see it.

    The row igni answered with, field for field: nothing renamed,
    nothing added, and no credential — the row says whether a key is
    configured, not where it comes from.
    """

    model_config = ConfigDict(extra="ignore")

    model_id: str
    provider: str
    url: str | None
    #: Whether the entry says where its key comes from. Never the key,
    #: never the environment variable, never the command.
    has_api_key: bool
    temperature: float | None
    max_tokens: int | None
    timeout: int
    vision: bool
    context_window: int | None
    thinking: dict[str, WireJson] | None
    effort: str | None
    prompt_caching: bool
    source: str

    @classmethod
    def of(cls, entry: ModelRegistryEntryView) -> ModelEntry:
        """The registry's row, as igni answered with it."""
        return cls(
            model_id=entry.model_id,
            provider=entry.provider,
            url=entry.url,
            has_api_key=entry.has_api_key,
            temperature=entry.temperature,
            max_tokens=entry.max_tokens,
            timeout=entry.timeout,
            vision=entry.vision,
            context_window=entry.context_window,
            thinking=entry.thinking,
            effort=entry.effort,
            prompt_caching=entry.prompt_caching,
            source=entry.source,
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
