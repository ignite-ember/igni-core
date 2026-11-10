"""An extension's tools are judged by the table the session was given.

The extension installer fills a table of what an extension's tools say
they are — read-only, destructive, open-world — and every permission
mode reads it: a read-only tool runs where a writing one waits for the
person. The table belongs to the process, but the session that reads it
is *handed* one, the same way it is handed the prompt contributions and
the prompt loader. A builder that reached for the global instead would
make the session's decisions depend on whatever else in the process had
touched the global.

The table in this test is the session's own, so the test does not
depend on what any other test in this process has declared.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.core.config.permission_eval import PermissionDecision
from igni_core.core.config.permission_eval.schemas import PermissionMode
from igni_core.core.mcp.hints import ToolHints, ToolHintsRegistry
from tests.session_factory import build_session

#: Reads, and only from its own system.
LOCAL_READ = ToolHints(read_only=True, destructive=False, idempotent=True, open_world=False)

HINTED = "mcp_ext:test.a_local_read"
UNHINTED = "mcp_ext:test.a_tool_nobody_described"


class RefusesToJudge:
    """A judge that cannot answer.

    Bypass's last check asks the model about a call it cannot decide by
    rules. With a judge that cannot answer, such a call is put to the
    person — so the guard's own answer is observable, and the test does
    not wait on a model.
    """

    async def score(self, text: str):
        raise RuntimeError("no model to judge with")


@pytest.fixture
def table() -> ToolHintsRegistry:
    table = ToolHintsRegistry()
    table.declare("test", {HINTED: LOCAL_READ})
    return table


@pytest.fixture
def session(tmp_path: Path, table: ToolHintsRegistry, monkeypatch):
    monkeypatch.setattr("igni_core.core.hooks.bypass_guard.HarmJudge", RefusesToJudge)
    return build_session(tmp_path, tool_hints=table)


def test_the_modes_read_the_table_the_session_was_given(session) -> None:
    """A tool the table calls read-only runs in plan mode; an undescribed
    one waits for the person."""
    session.mode.set_permission_mode(PermissionMode.PLAN.value)

    assert session.permission_evaluator.evaluate(HINTED, {}) is PermissionDecision.ALLOW
    assert session.permission_evaluator.evaluate(UNHINTED, {}) is PermissionDecision.DEFER


async def test_bypass_waves_through_the_tool_the_table_calls_a_local_read(session) -> None:
    """Bypass's last check reads the same table: a local read is igni's own
    kind of read, and runs."""
    session.mode.set_permission_mode(PermissionMode.BYPASS_PERMISSIONS.value)
    guard = session.tool_hook_factory.bypass_guard

    assert guard is not None
    assert await guard.check(HINTED, {}) is None


async def test_a_tool_the_table_does_not_call_a_local_read_is_asked_about(session) -> None:
    """The control: without the exemption the call is put to the person."""
    session.mode.set_permission_mode(PermissionMode.BYPASS_PERMISSIONS.value)
    guard = session.tool_hook_factory.bypass_guard

    assert guard is not None
    assert await guard.check(UNHINTED, {}) is not None


def test_a_session_given_nothing_gets_the_process_table(tmp_path: Path) -> None:
    """The fallback is the process's own table — the one the installer
    fills — and it is chosen once, at the session's foundation."""
    session = build_session(tmp_path)

    assert session.tool_hints is ToolHintsRegistry.shared()
