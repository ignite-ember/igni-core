"""The hooks RPCs answer what the session's hooks are.

``get_hooks_details`` lists one row per hook the session runs, an empty
list when there are none, and ``reload_hooks`` re-reads the
configuration and says in an ``info`` message how many hooks are active.
Both go through the real panels controller; only the session is a stand
in.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from igni_core.backend.panels.hooks_panel import HooksPanelController
from igni_core.backend.rpc_handlers.hooks import HooksRpcHandler
from igni_core.core.hooks.schemas import HookDefinition
from igni_core.protocol.rpc import RpcMethod


def _handler(session: SimpleNamespace) -> HooksRpcHandler:
    panel = HooksPanelController(session)  # type: ignore[arg-type]
    backend = SimpleNamespace(
        get_hooks_details=panel.snapshot,
        controllers=SimpleNamespace(panels=SimpleNamespace(reload_hooks=panel.reload)),
    )
    return HooksRpcHandler(SimpleNamespace(backend=backend))  # type: ignore[arg-type]


def test_every_hook_the_session_runs_is_listed_with_what_it_does() -> None:
    session = SimpleNamespace(
        hooks_map={
            "PreToolUse": [
                HookDefinition(type="command", command="lint.sh", matcher="Bash", timeout=500),
                HookDefinition(type="http", url="https://hooks.acme.io/x", headers={"B": "1"}),
            ],
            "SessionStart": [HookDefinition(type="command", command="boot.sh", background=True)],
        }
    )

    rows = _handler(session).methods()[RpcMethod.GET_HOOKS_DETAILS]({})

    assert [(r.event, r.type) for r in rows] == [
        ("PreToolUse", "command"),
        ("PreToolUse", "http"),
        ("SessionStart", "command"),
    ]
    assert (rows[0].command, rows[0].matcher, rows[0].timeout_ms) == ("lint.sh", "Bash", 500)
    assert rows[1].url == "https://hooks.acme.io/x"
    assert rows[1].header_names == ["B"]
    assert rows[2].background is True
    assert rows[2].matcher == ""


def test_a_session_with_no_hooks_lists_none() -> None:
    session = SimpleNamespace(hooks_map={})

    assert _handler(session).methods()[RpcMethod.GET_HOOKS_DETAILS]({}) == []


@pytest.mark.parametrize("count", [0, 1, 7])
def test_reloading_says_how_many_hooks_are_active(count: int) -> None:
    reloads: list[int] = []
    session = SimpleNamespace(hooks_map={}, reload_hooks=lambda: (reloads.append(count), count)[1])

    info = _handler(session).methods()[RpcMethod.RELOAD_HOOKS]({})

    assert info.text == f"Reloaded hooks — {count} active hook(s) across all events."
    assert reloads == [count]
