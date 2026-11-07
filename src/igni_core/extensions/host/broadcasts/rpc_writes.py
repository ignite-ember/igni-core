"""Writes made through RPC, whoever made them, as what they changed.

A window's write broadcasts nothing of its own, so "every write is a
signal" holds only because each successful RPC is told here
(:class:`RpcWrite`) and translated — one table, one handler per method.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from pydantic import BaseModel

from igni_core.extensions.host.broadcasts.deferred_reads import DeferredReads
from igni_core.extensions.host.broadcasts.signal import Emitter, Signal, merged
from igni_core.extensions.host.broadcasts.task_signals import TaskSignals
from igni_core.extensions.registry import ExtensionRegistry
from igni_core.extensions.store import ExtensionStore
from igni_core.protocol.rpc import RpcMethod


class RpcWrite(BaseModel):
    """One RPC that succeeded: which, with what, in which session."""

    method: RpcMethod
    #: The RPC's arguments: each method takes its own.
    args: dict[str, Any]
    session_id: str

    @property
    def subject(self) -> Any:
        """What the write was to — a server, a plugin, an agent — as the caller named it."""
        return self.args.get("name") or self.args.get("server_name")

    def text(self, key: str) -> str:
        return str(self.args.get(key, ""))


class ModelsChanged(Signal):
    WIRE: ClassVar[str] = "models.changed"

    current: str


class WriteChanged(Signal):
    """A ``CollectionChange``: something added, removed or toggled in a collection."""

    what: str
    name: Any = None
    payload: None = None

    @classmethod
    def written(cls, write: RpcWrite, what: str) -> WriteChanged:
        return cls(what=what, name=write.subject)


class McpChanged(WriteChanged):
    WIRE: ClassVar[str] = "mcp.changed"


class PluginsChanged(WriteChanged):
    WIRE: ClassVar[str] = "plugins.changed"

    @classmethod
    def written(cls, write: RpcWrite, what: str) -> WriteChanged:
        return cls(what=what, name=write.subject or write.args.get("url") or write.args.get("ref"))


class AgentsChanged(WriteChanged):
    WIRE: ClassVar[str] = "agents.changed"


class HooksChanged(WriteChanged):
    WIRE: ClassVar[str] = "hooks.changed"

    @classmethod
    def written(cls, write: RpcWrite, what: str) -> WriteChanged:
        return cls(what=what)


class HistoryChanged(Signal):
    WIRE: ClassVar[str] = "chat.history_changed"

    session_id: str
    what: str
    run_id: str


class SettingsChanged(Signal):
    WIRE: ClassVar[str] = "self.settings_changed"

    values: dict[str, Any]


class ExtensionSettings:
    """An extension's saved settings, read back after a write."""

    def __init__(self, registry: ExtensionRegistry, store: ExtensionStore) -> None:
        self._registry = registry
        self._store = store

    def changed(self, extension_id: str) -> SettingsChanged | None:
        entry = self._registry.get(extension_id)
        if entry is None:
            return None
        return SettingsChanged(values=dict(self._store.settings(entry.manifest)))


class RpcWriteSignals:
    """Translates each RPC write through one table."""

    #: Writes told as a change to a collection, and what each one did.
    CHANGES: ClassVar[dict[RpcMethod, tuple[type[WriteChanged], str]]] = {
        RpcMethod.MCP_CONNECT: (McpChanged, "connected"),
        RpcMethod.MCP_DISCONNECT: (McpChanged, "disconnected"),
        RpcMethod.SET_MCP_TOOL_ENABLED: (McpChanged, "tool_toggled"),
        RpcMethod.ENSURE_MCP: (McpChanged, "ensured"),
        RpcMethod.INSTALL_PLUGIN: (PluginsChanged, "installed"),
        RpcMethod.UPDATE_PLUGIN: (PluginsChanged, "updated"),
        RpcMethod.REMOVE_PLUGIN: (PluginsChanged, "removed"),
        RpcMethod.SET_PLUGIN_ENABLED: (PluginsChanged, "toggled"),
        RpcMethod.ADD_MARKETPLACE: (PluginsChanged, "marketplace_added"),
        RpcMethod.REMOVE_MARKETPLACE: (PluginsChanged, "marketplace_removed"),
        RpcMethod.REFRESH_MARKETPLACES: (PluginsChanged, "marketplaces_refreshed"),
        RpcMethod.PROMOTE_EPHEMERAL_AGENT: (AgentsChanged, "promoted"),
        RpcMethod.DISCARD_EPHEMERAL_AGENT: (AgentsChanged, "discarded"),
        RpcMethod.RELOAD_HOOKS: (HooksChanged, "reloaded"),
    }
    #: Writes to the session's history: what each did, and the argument naming the run.
    HISTORY: ClassVar[dict[RpcMethod, tuple[str, str]]] = {
        RpcMethod.TRUNCATE_HISTORY: ("truncated", "run_id"),
        RpcMethod.DISCARD_INTERRUPTED_RUN: ("discarded", "message_id"),
    }
    #: Writes after which the loop is read again.
    LOOP_WRITES: ClassVar[frozenset[RpcMethod]] = frozenset(
        {
            RpcMethod.LOOP_PAUSE,
            RpcMethod.LOOP_RESUME,
            RpcMethod.CANCEL_PENDING_LOOP,
            RpcMethod.POP_PENDING_LOOP_ITERATION,
        }
    )
    #: Writes after which the account is read again.
    ACCOUNT_WRITES: ClassVar[frozenset[RpcMethod]] = frozenset(
        {RpcMethod.CLEAR_CLOUD_CREDENTIALS, RpcMethod.RELOAD_CLOUD_CREDENTIALS}
    )

    def __init__(
        self,
        emitter: Emitter,
        reads: DeferredReads,
        tasks: TaskSignals,
        settings: ExtensionSettings,
        session_switched: Callable[[str, str | None], None],
    ) -> None:
        self._emitter = emitter
        self._settings = settings
        self._session_switched = session_switched
        self._table: dict[RpcMethod, Callable[[RpcWrite], None]] = merged(
            {
                RpcMethod.TOGGLE_VERBOSE: lambda w: reads.display(w.session_id),
                RpcMethod.SWITCH_MODEL: self._model,
                RpcMethod.SET_EXTENSION_SETTINGS: self._extension_settings,
                RpcMethod.CANCEL_SCHEDULED_TASK: lambda w: tasks.cancelled(w.text("task_id")),
                RpcMethod.SWITCH_SESSION: self._switched,
            },
            dict.fromkeys(self.CHANGES, self._changed),
            dict.fromkeys(self.HISTORY, self._history),
            dict.fromkeys(self.LOOP_WRITES, lambda w: reads.loop(w.session_id)),
            dict.fromkeys(self.ACCOUNT_WRITES, lambda w: reads.account(w.session_id)),
        )

    def handle(self, write: RpcWrite) -> None:
        """A write succeeded — whoever made it. Signal what it changed."""
        handler = self._table.get(write.method)
        if handler is not None:
            handler(write)

    def _model(self, write: RpcWrite) -> None:
        self._emitter.send(ModelsChanged(current=write.text("model_name")))

    def _changed(self, write: RpcWrite) -> None:
        signal, what = self.CHANGES[write.method]
        self._emitter.send(signal.written(write, what))

    def _history(self, write: RpcWrite) -> None:
        what, run_arg = self.HISTORY[write.method]
        self._emitter.send(
            HistoryChanged(session_id=write.session_id, what=what, run_id=write.text(run_arg))
        )

    def _extension_settings(self, write: RpcWrite) -> None:
        extension_id = write.text("extension_id")
        changed = self._settings.changed(extension_id)
        if changed is not None:
            self._emitter.send(changed, only=extension_id)

    def _switched(self, write: RpcWrite) -> None:
        self._session_switched(write.text("session_id"), None)


__all__ = [
    "AgentsChanged",
    "ExtensionSettings",
    "HistoryChanged",
    "HooksChanged",
    "McpChanged",
    "ModelsChanged",
    "PluginsChanged",
    "RpcWrite",
    "RpcWriteSignals",
    "SettingsChanged",
    "WriteChanged",
]
