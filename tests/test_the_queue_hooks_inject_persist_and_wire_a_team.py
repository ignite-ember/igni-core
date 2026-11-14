"""The queue hooks' contract: the note text, the buffer, the persisted messages, the team wiring.

The implementation lives in :mod:`igni_core.core.hooks.queue`. The flow tests are in
``test_queue_hook.py``; this file pins the exact bytes, the edges and the package surface.
"""

import inspect
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from agno.run.agent import RunOutput
from agno.utils.hooks import filter_hook_args

import igni_core.core.hooks.queue as queue_package
from igni_core.core.hooks.queue import (
    USER_NOTE_HEADER,
    AgnoCallableAdapter,
    InjectedMessage,
    InjectedNote,
    InjectedRunBuffer,
    QueueBridge,
    QueueCallbacks,
    QueueInjectorHook,
    QueuePersisterHook,
    SupportsRunOutput,
    TakenMessage,
    TakesMessages,
)


class _TakingQueue:
    """A queue of ids and texts that hands them out with take(), as a durable one does."""

    def __init__(self, *items: tuple[str, str]) -> None:
        self._items = list(items)

    def __bool__(self) -> bool:
        return bool(self._items)

    def take(self) -> TakenMessage:
        message_id, text = self._items.pop(0)
        return TakenMessage(id=message_id, text=text)


def test_the_package_exports_its_fourteen_names_and_each_resolves() -> None:
    assert sorted(queue_package.__all__) == sorted(
        [
            "USER_NOTE_HEADER",
            "AgnoCallableAdapter",
            "InjectedMessage",
            "InjectedNote",
            "InjectedRunBuffer",
            "PostHookInvocation",
            "QueueBridge",
            "QueueCallbacks",
            "QueueInjectorHook",
            "QueuePersisterHook",
            "SupportsRunOutput",
            "TakenMessage",
            "TakesMessages",
            "ToolHookInvocation",
        ]
    )
    for name in queue_package.__all__:
        assert getattr(queue_package, name) is not None


# ── the adapter ──────────────────────────────────────────────────────


def test_an_injector_is_a_coroutine_function_and_a_persister_is_not() -> None:
    bridge = QueueBridge(queue=[])
    assert inspect.iscoroutinefunction(bridge.injector)
    assert not inspect.iscoroutinefunction(bridge.persister)


def test_a_hook_instance_carries_its_class_name_for_agno_telemetry() -> None:
    bridge = QueueBridge(queue=[])
    assert bridge.injector.__name__ == "QueueInjectorHook"
    assert bridge.persister.__name__ == "QueuePersisterHook"

    class Custom(AgnoCallableAdapter):
        pass

    assert Custom().__name__ == "Custom"


def test_agno_hands_the_injector_its_name_func_args_and_agent() -> None:
    injector = QueueBridge(queue=[]).injector
    all_args = {
        "name": "t",
        "func": lambda: 1,
        "function": "x",
        "function_call": "y",
        "args": {"a": 1},
        "agent": "ag",
        "debug_mode": False,
    }
    handed = filter_hook_args(injector, all_args)
    for key in ("name", "func", "args", "agent"):
        assert handed[key] is all_args[key]
    assert list(inspect.signature(injector).parameters)[:4] == ["name", "func", "args", "agent"]


# ── the buffer ───────────────────────────────────────────────────────


def test_the_buffer_keeps_an_explicit_id_and_makes_a_unique_one_otherwise() -> None:
    buffer = InjectedRunBuffer()
    kept = buffer.append("a", message_id="row-7")
    assert kept.id == "row-7"
    first = buffer.append("b")
    second = buffer.append("c", message_id=None)
    third = buffer.append("d", message_id="")
    ids = {first.id, second.id, third.id}
    assert len(ids) == 3
    for generated in ids:
        assert str(uuid.UUID(generated)) == generated


def test_the_buffer_keeps_order_and_reports_its_size() -> None:
    buffer = InjectedRunBuffer()
    assert buffer.is_empty()
    assert len(buffer) == 0
    returned = buffer.append("é ünï\ncode ✓")
    assert returned is buffer.items[0]
    buffer.append("")
    assert not buffer.is_empty()
    assert len(buffer) == 2
    assert [m.text for m in buffer.items] == ["é ünï\ncode ✓", ""]


def test_a_buffer_snapshot_is_a_copy_and_clear_empties_the_buffer() -> None:
    buffer = InjectedRunBuffer()
    buffer.append("a")
    snapshot = buffer.snapshot()
    snapshot.clear()
    buffer.append("b")
    assert [m.text for m in buffer.snapshot()] == ["a", "b"]
    buffer.clear()
    assert buffer.is_empty()
    buffer.clear()
    assert len(buffer) == 0


# ── the schemas ──────────────────────────────────────────────────────


def test_the_note_header_is_the_exact_wire_string() -> None:
    assert USER_NOTE_HEADER == "USER MESSAGE WHILE YOU WERE WORKING"
    assert InjectedNote(messages=[]).header == USER_NOTE_HEADER


def test_the_note_renders_exact_bytes_onto_none_text_and_other_results() -> None:
    note = InjectedNote(messages=[InjectedMessage(text="a"), InjectedMessage(text="b")])
    block = "[USER MESSAGE WHILE YOU WERE WORKING]\n- a\n- b\n[END USER MESSAGE]"
    assert note.render_onto(None) == block
    assert note.render_onto("result") == "result\n\n" + block
    assert note.render_onto({"k": "{v}"}) == "{'k': '{v}'}\n\n" + block
    assert note.render_onto("") == "\n\n" + block


def test_a_note_with_no_messages_renders_an_empty_block_and_keeps_newlines_in_text() -> None:
    assert (
        InjectedNote(messages=[]).render_onto("r")
        == "r\n\n[USER MESSAGE WHILE YOU WERE WORKING]\n\n[END USER MESSAGE]"
    )
    multi = InjectedNote(messages=[InjectedMessage(text="l1\nl2 ✓")])
    assert multi.render_onto("r").endswith("\n- l1\nl2 ✓\n[END USER MESSAGE]")


def test_an_injected_message_gets_a_unique_id_and_a_utc_timestamp() -> None:
    one, two = InjectedMessage(text="x"), InjectedMessage(text="x")
    assert one.id != two.id
    assert str(uuid.UUID(one.id)) == one.id
    assert one.created_at.utcoffset() == timedelta(0)
    assert InjectedMessage(text="x", id="q-1").id == "q-1"


def test_callbacks_that_are_not_set_do_nothing_and_set_ones_are_called() -> None:
    QueueCallbacks().notify_inject("x")
    QueueCallbacks().notify_queue_changed()
    seen: list[object] = []
    callbacks = QueueCallbacks(
        on_inject=lambda text: seen.append(("inject", text)),
        on_queue_changed=lambda: seen.append("changed"),
    )
    callbacks.notify_inject("hi")
    callbacks.notify_queue_changed()
    assert seen == [("inject", "hi"), "changed"]


def test_the_protocols_tell_conforming_objects_from_the_rest() -> None:
    assert isinstance(SimpleNamespace(messages=None), SupportsRunOutput)
    assert isinstance(RunOutput(run_id="r", session_id="s"), SupportsRunOutput)
    assert not isinstance(SimpleNamespace(), SupportsRunOutput)
    assert isinstance(_TakingQueue(), TakesMessages)
    assert not isinstance([], TakesMessages)


# ── the injector ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_injector_suffixes_the_exact_note_in_queue_order() -> None:
    queue = ["a", "b"]
    result = await QueueBridge(queue=queue).injector(name="t", func=lambda: "result", args={})
    assert result == (
        "result\n\n[USER MESSAGE WHILE YOU WERE WORKING]\n- a\n- b\n[END USER MESSAGE]"
    )
    assert queue == []


@pytest.mark.asyncio
async def test_the_injector_with_no_tool_still_delivers_the_note() -> None:
    result = await QueueBridge(queue=["hi"]).injector(name="t")
    assert result == "[USER MESSAGE WHILE YOU WERE WORKING]\n- hi\n[END USER MESSAGE]"


@pytest.mark.asyncio
async def test_the_injector_defaults_missing_args_and_ignores_unknown_keywords() -> None:
    injector = QueueBridge(queue=[]).injector
    assert await injector(name="t", func=lambda: "ok", args=None, debug_mode=True, extra=1) == "ok"


@pytest.mark.asyncio
async def test_the_injector_fires_each_callback_in_order_and_changed_once() -> None:
    events: list[object] = []
    bridge = QueueBridge(
        queue=["one", "two", "three"],
        on_inject=lambda text: events.append(text),
        on_queue_changed=lambda: events.append("changed"),
    )
    await bridge.injector(name="t", func=lambda: None, args={})
    assert events == ["one", "two", "three", "changed"]
    events.clear()
    await bridge.injector(name="t", func=lambda: None, args={})
    assert events == []


@pytest.mark.asyncio
async def test_a_tool_that_raises_propagates_and_leaves_the_queue_for_the_next_call() -> None:
    queue = ["wait"]
    bridge = QueueBridge(queue=queue)

    def boom() -> None:
        raise RuntimeError("tool failed")

    with pytest.raises(RuntimeError, match="tool failed"):
        await bridge.injector(name="t", func=boom, args={})
    assert queue == ["wait"]
    assert bridge.buffer.is_empty()
    result = await bridge.injector(name="t", func=lambda: "ok", args={})
    assert "- wait" in result


@pytest.mark.asyncio
async def test_a_message_queued_while_the_tool_ran_is_drained_in_the_same_call() -> None:
    queue = ["early"]

    async def tool() -> str:
        queue.append("during")
        return "done"

    bridge = QueueBridge(queue=queue)
    result = await bridge.injector(name="t", func=tool, args={})
    assert result.endswith("- early\n- during\n[END USER MESSAGE]")
    assert queue == []


@pytest.mark.asyncio
async def test_a_failing_callback_propagates_after_the_buffer_holds_the_messages() -> None:
    def refuse(_text: str) -> None:
        raise ValueError("ui down")

    bridge = QueueBridge(queue=["x"], on_inject=refuse)
    with pytest.raises(ValueError, match="ui down"):
        await bridge.injector(name="t", func=lambda: "r", args={})
    assert bridge.injector.injected_this_run == ["x"]


@pytest.mark.asyncio
async def test_a_durable_queue_gives_the_buffer_its_ids_and_an_empty_id_gets_a_generated_one() -> (
    None
):
    queue = _TakingQueue(("row-1", "first"), ("", "second"))
    bridge = QueueBridge(queue=queue)  # type: ignore[arg-type]
    result = await bridge.injector(name="t", func=lambda: "r", args={})
    assert result.endswith("- first\n- second\n[END USER MESSAGE]")
    ids = [m.id for m in bridge.buffer.items]
    assert ids[0] == "row-1"
    assert ids[1] != "" and str(uuid.UUID(ids[1])) == ids[1]


# ── the persister ────────────────────────────────────────────────────


def test_the_persister_appends_after_history_with_the_queue_id_then_a_second_call_adds_nothing() -> (
    None
):
    bridge = QueueBridge(queue=[])
    bridge.buffer.append("typed", message_id="row-9")
    history = SimpleNamespace(role="assistant", content="earlier")
    run_output = SimpleNamespace(messages=[history])
    bridge.persister(run_output=run_output, agent="ignored", session="ignored")
    assert run_output.messages[0] is history
    appended = run_output.messages[1]
    assert (appended.id, appended.role, appended.content) == ("row-9", "user", "typed")
    assert appended.add_to_agent_memory is True
    bridge.persister(run_output=run_output)
    assert len(run_output.messages) == 2


def test_the_persister_clears_the_buffer_for_a_run_output_without_messages() -> None:
    bridge = QueueBridge(queue=[])
    bridge.buffer.append("lost")
    bare = SimpleNamespace()
    bridge.persister(run_output=bare)
    assert not hasattr(bare, "messages")
    assert bridge.buffer.is_empty()


def test_a_failure_while_appending_keeps_the_buffer_for_a_retry() -> None:
    bridge = QueueBridge(queue=[])
    bridge.buffer.append("keep me")

    class Refusing:
        messages = None

        def __setattr__(self, key: str, value: object) -> None:
            raise OSError("read only")

    with pytest.raises(OSError, match="read only"):
        bridge.persister(run_output=Refusing())
    assert [m.text for m in bridge.buffer.items] == ["keep me"]


# ── the bridge ───────────────────────────────────────────────────────


def test_register_on_puts_the_hooks_after_the_ones_the_team_has() -> None:
    first_tool, first_post = object(), object()
    team = SimpleNamespace(tool_hooks=[first_tool], post_hooks=[first_post])
    bridge = QueueBridge(queue=[])
    bridge.register_on(team)
    assert team.tool_hooks == [first_tool, bridge.injector]
    assert team.post_hooks == [first_post, bridge.persister]


def test_register_on_treats_missing_hook_lists_as_empty_and_twice_registers_twice() -> None:
    team = SimpleNamespace(tool_hooks=None, post_hooks=None)
    bridge = QueueBridge(queue=[])
    bridge.register_on(team)
    assert team.tool_hooks == [bridge.injector]
    assert team.post_hooks == [bridge.persister]
    bridge.register_on(team)
    assert team.tool_hooks == [bridge.injector, bridge.injector]


@pytest.mark.asyncio
async def test_the_bridge_buffer_is_the_one_both_hooks_use() -> None:
    bridge = QueueBridge(queue=["shared"])
    assert isinstance(bridge.buffer, InjectedRunBuffer)
    await bridge.injector(name="t", func=lambda: "r", args={})
    assert [m.text for m in bridge.buffer.items] == ["shared"]
    run_output = SimpleNamespace(messages=[])
    bridge.persister(run_output=run_output)
    assert [m.content for m in run_output.messages] == ["shared"]
    assert bridge.buffer.is_empty()
    assert isinstance(bridge.injector, QueueInjectorHook)
    assert isinstance(bridge.persister, QueuePersisterHook)
