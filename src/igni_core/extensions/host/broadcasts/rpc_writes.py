"""Writes made through RPC, whoever made them, as what they changed.

A window's write broadcasts nothing of its own, so "every write is a
signal" holds only because each successful RPC is told here
(:class:`~igni_core.extensions.host.rpc_write.RpcWrite`) and translated — one table, one handler per method.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict

from igni_core.extensions.host.broadcasts.deferred_reads import DeferredReads
from igni_core.extensions.host.broadcasts.signal import merged
from igni_core.extensions.host.broadcasts.task_signals import TaskSignals
from igni_core.extensions.host.rpc_write import RpcWrite
from igni_core.extensions.host.signal_values import Emitter, Signal
from igni_core.protocol.rpc import RpcMethod


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


class SavedSettings(Protocol):
    """An extension's saved settings, read back after a write —
    :class:`~igni_core.extensions.host.extension_directory.ExtensionDirectory` is one."""

    def saved_settings(self, extension_id: str) -> dict[str, Any] | None: ...


class CollectionWrite(BaseModel):
    """A write told as a change to a collection: which signal, and what it did."""

    model_config = ConfigDict(frozen=True)

    signal: type[WriteChanged]
    what: str


class HistoryWrite(BaseModel):
    """A write to the session's history: what it did, and the argument naming the run."""

    model_config = ConfigDict(frozen=True)

    what: str
    run_arg: str


class RpcWriteSignals:
    """Translates each RPC write through one table."""

    #: Writes told as a change to a collection, and what each one did.
    CHANGES: ClassVar[Mapping[RpcMethod, CollectionWrite]] = {
        RpcMethod.MCP_CONNECT: CollectionWrite(signal=McpChanged, what="connected"),
        RpcMethod.MCP_DISCONNECT: CollectionWrite(signal=McpChanged, what="disconnected"),
        RpcMethod.SET_MCP_TOOL_ENABLED: CollectionWrite(signal=McpChanged, what="tool_toggled"),
        RpcMethod.ENSURE_MCP: CollectionWrite(signal=McpChanged, what="ensured"),
        RpcMethod.INSTALL_PLUGIN: CollectionWrite(signal=PluginsChanged, what="installed"),
        RpcMethod.UPDATE_PLUGIN: CollectionWrite(signal=PluginsChanged, what="updated"),
        RpcMethod.REMOVE_PLUGIN: CollectionWrite(signal=PluginsChanged, what="removed"),
        RpcMethod.SET_PLUGIN_ENABLED: CollectionWrite(signal=PluginsChanged, what="toggled"),
        RpcMethod.ADD_MARKETPLACE: CollectionWrite(signal=PluginsChanged, what="marketplace_added"),
        RpcMethod.REMOVE_MARKETPLACE: CollectionWrite(
            signal=PluginsChanged, what="marketplace_removed"
        ),
        RpcMethod.REFRESH_MARKETPLACES: CollectionWrite(
            signal=PluginsChanged, what="marketplaces_refreshed"
        ),
        RpcMethod.PROMOTE_EPHEMERAL_AGENT: CollectionWrite(signal=AgentsChanged, what="promoted"),
        RpcMethod.DISCARD_EPHEMERAL_AGENT: CollectionWrite(signal=AgentsChanged, what="discarded"),
        RpcMethod.RELOAD_HOOKS: CollectionWrite(signal=HooksChanged, what="reloaded"),
    }
    #: Writes to the session's history.
    HISTORY: ClassVar[Mapping[RpcMethod, HistoryWrite]] = {
        RpcMethod.TRUNCATE_HISTORY: HistoryWrite(what="truncated", run_arg="run_id"),
        RpcMethod.DISCARD_INTERRUPTED_RUN: HistoryWrite(what="discarded", run_arg="message_id"),
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
        settings: SavedSettings,
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
        change = self.CHANGES[write.method]
        self._emitter.send(change.signal.written(write, change.what))

    def _history(self, write: RpcWrite) -> None:
        history = self.HISTORY[write.method]
        self._emitter.send(
            HistoryChanged(
                session_id=write.session_id, what=history.what, run_id=write.text(history.run_arg)
            )
        )

    def _extension_settings(self, write: RpcWrite) -> None:
        extension_id = write.text("extension_id")
        values = self._settings.saved_settings(extension_id)
        if values is not None:
            self._emitter.send(SettingsChanged(values=values), only=extension_id)

    def _switched(self, write: RpcWrite) -> None:
        self._session_switched(write.text("session_id"), None)


__all__ = [
    "AgentsChanged",
    "CollectionWrite",
    "HistoryChanged",
    "HistoryWrite",
    "HooksChanged",
    "McpChanged",
    "ModelsChanged",
    "PluginsChanged",
    "RpcWriteSignals",
    "SavedSettings",
    "SettingsChanged",
    "WriteChanged",
]
