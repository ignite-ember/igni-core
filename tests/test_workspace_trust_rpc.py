"""Workspace trust from the desktop and web: the RPCs behind "Trust this folder?".

``get_workspace_trust`` says where a folder stands; ``set_workspace_trust``
records Trust / Don't trust / This time only, pushes the new status so other
windows close their dialog, and rebuilds the running session when trust
changed. The organisation's policy can refuse, and the refusal is the RPC's
error. And a running session really does pick the change up: a trusted
folder's hooks and permission rules come in, a revoked one's go.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from igni_core.backend.rpc_binding import RpcArgsError
from igni_core.backend.rpc_handlers.workspace_trust import WorkspaceTrustRpcHandler
from igni_core.backend.workspace_trust_controller import (
    WorkspaceTrustController,
    WorkspaceTrustRefusedError,
    reload_settings,
)
from igni_core.core.paths import CONFIG_DIR
from igni_core.core.workspace_trust import WorkspaceIdentity, process_trust
from igni_core.core.workspace_trust.policy import WorkspaceTrustConfig
from igni_core.protocol.rpc import RpcMethod

pytestmark = pytest.mark.workspace_trust


def _handler(folder: Path) -> tuple[WorkspaceTrustRpcHandler, MagicMock]:
    backend = MagicMock()
    backend.workspace_trust = WorkspaceTrustController(folder)
    backend.apply_workspace_trust = AsyncMock()
    ctx = MagicMock()
    ctx.backend = backend
    return WorkspaceTrustRpcHandler(ctx), ctx


def _stored(folder: Path) -> str | None:
    return process_trust().store.get(WorkspaceIdentity.of(folder))


def test_both_rpcs_are_registered(tmp_path: Path) -> None:
    handler, _ = _handler(tmp_path)
    assert set(handler.methods()) == {
        RpcMethod.GET_WORKSPACE_TRUST,
        RpcMethod.SET_WORKSPACE_TRUST,
    }


def test_a_fresh_folder_asks(tmp_path: Path) -> None:
    handler, _ = _handler(tmp_path)
    status = handler.methods()[RpcMethod.GET_WORKSPACE_TRUST]({})
    assert status.state == "ask"
    assert status.can_trust is True
    assert status.path == str(tmp_path.resolve())
    assert status.remote is None
    assert status.policy_mode == "ask"
    assert "hooks" in status.gated


async def test_trust_is_recorded_pushed_and_applied(tmp_path: Path) -> None:
    handler, ctx = _handler(tmp_path)
    status = await handler.methods()[RpcMethod.SET_WORKSPACE_TRUST]({"decision": "trust"})
    assert status.state == "trusted"
    assert handler.methods()[RpcMethod.GET_WORKSPACE_TRUST]({}).state == "trusted"
    assert _stored(tmp_path) == "trusted"
    ctx.backend.apply_workspace_trust.assert_awaited_once()
    ctx.push.workspace_trust.assert_called_once()
    assert ctx.push.workspace_trust.call_args.args[0].state == "trusted"


async def test_this_time_only_trusts_without_recording(tmp_path: Path) -> None:
    handler, ctx = _handler(tmp_path)
    status = await handler.methods()[RpcMethod.SET_WORKSPACE_TRUST]({"decision": "once"})
    assert status.state == "trusted"
    assert _stored(tmp_path) is None
    ctx.backend.apply_workspace_trust.assert_awaited_once()


async def test_dont_trust_is_recorded_and_changes_nothing_running(tmp_path: Path) -> None:
    handler, ctx = _handler(tmp_path)
    status = await handler.methods()[RpcMethod.SET_WORKSPACE_TRUST]({"decision": "distrust"})
    assert status.state == "untrusted"
    assert _stored(tmp_path) == "untrusted"
    # Untrusted before (unset) and after: nothing to rebuild.
    ctx.backend.apply_workspace_trust.assert_not_awaited()
    ctx.push.workspace_trust.assert_called_once()


async def test_distrusting_a_trusted_folder_rebuilds(tmp_path: Path) -> None:
    handler, ctx = _handler(tmp_path)
    await handler.methods()[RpcMethod.SET_WORKSPACE_TRUST]({"decision": "trust"})
    await handler.methods()[RpcMethod.SET_WORKSPACE_TRUST]({"decision": "distrust"})
    assert handler.methods()[RpcMethod.GET_WORKSPACE_TRUST]({}).state == "untrusted"
    assert ctx.backend.apply_workspace_trust.await_count == 2


async def test_policy_never_refuses_with_its_reason(tmp_path: Path) -> None:
    process_trust().set_policy(WorkspaceTrustConfig(mode="never"))
    handler, ctx = _handler(tmp_path)
    status = handler.methods()[RpcMethod.GET_WORKSPACE_TRUST]({})
    assert status.state == "untrusted"
    assert status.can_trust is False
    with pytest.raises(WorkspaceTrustRefusedError, match="never"):
        await handler.methods()[RpcMethod.SET_WORKSPACE_TRUST]({"decision": "trust"})
    with pytest.raises(WorkspaceTrustRefusedError):
        await handler.methods()[RpcMethod.SET_WORKSPACE_TRUST]({"decision": "once"})
    assert _stored(tmp_path) is None
    ctx.backend.apply_workspace_trust.assert_not_awaited()


async def test_an_unknown_answer_is_refused(tmp_path: Path) -> None:
    handler, _ = _handler(tmp_path)
    with pytest.raises(RpcArgsError, match="'trust', 'distrust' or 'once'"):
        await handler.methods()[RpcMethod.SET_WORKSPACE_TRUST]({"decision": "maybe"})


# ── A running session picks the change up ─────────────────────────

_STUB_MODEL: dict[str, Any] = {
    "default": "stub",
    "registry": {
        "stub": {
            "provider": "openai_like",
            "model_id": "stub-1",
            "url": "http://localhost:9/v1",
            "api_key": "x",
            "context_window": 8192,
        }
    },
}


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    config = project / CONFIG_DIR
    config.mkdir()
    # What any session needs, from the user's config (not the folder's).
    home_config = Path.home() / CONFIG_DIR
    home_config.mkdir(parents=True, exist_ok=True)
    (home_config / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "code_index": {"enabled": False},
                "knowledge": {"enabled": False},
                "models": _STUB_MODEL,
                "storage": {"data_dir": str(tmp_path / "data")},
            }
        )
    )
    # The folder's own: a hook and a permission rule.
    (config / "settings.json").write_text(
        json.dumps(
            {
                "permissions": {"deny": ["Bash(make deploy)"]},
                "hooks": {"PreToolUse": [{"type": "command", "command": "true # from the folder"}]},
            }
        )
    )
    return project


async def test_a_running_session_picks_up_trust_and_its_revocation(tmp_path: Path) -> None:
    from igni_core.core.session.core import Session

    project = _project(tmp_path)
    session = Session(settings=reload_settings(project), project_dir=project)
    session.mode.set_permission_mode("plan")

    def project_hooks() -> int:
        return sum(
            1
            for entries in session.hooks_map.values()
            for hook in entries
            if "from the folder" in str(getattr(hook, "command", ""))
        )

    def denies_make_deploy() -> bool:
        evaluator = session.tool_hook_factory.permission_evaluator
        return any("make deploy" in rule.display() for rule in evaluator.deny)

    assert project_hooks() == 0
    assert not denies_make_deploy()

    process_trust().trust(project)
    await session.apply_workspace_trust(reload_settings(project))
    assert project_hooks() == 1
    assert denies_make_deploy()
    # The mode the person chose survives the rebuild.
    assert session.permission_mode_value == "plan"

    process_trust().revoke(project)
    await session.apply_workspace_trust(reload_settings(project))
    assert project_hooks() == 0
    assert not denies_make_deploy()


@pytest.mark.parametrize("decision", ["Trust", " trust", "TRUST", "", None])
async def test_a_decision_is_matched_exactly(decision: object, tmp_path: Path) -> None:
    handler, ctx = _handler(tmp_path)
    with pytest.raises(RpcArgsError):
        await handler.methods()[RpcMethod.SET_WORKSPACE_TRUST]({"decision": decision})
    ctx.backend.apply_workspace_trust.assert_not_awaited()
    assert _stored(tmp_path) is None


async def test_the_answer_comes_back_without_a_push_channel(tmp_path: Path) -> None:
    handler, ctx = _handler(tmp_path)
    ctx.push = None
    status = await handler.methods()[RpcMethod.SET_WORKSPACE_TRUST]({"decision": "trust"})
    assert status.state == "trusted"
    assert _stored(tmp_path) == "trusted"
    ctx.backend.apply_workspace_trust.assert_awaited_once()


async def test_a_failed_rebuild_surfaces_and_nothing_is_pushed(tmp_path: Path) -> None:
    handler, ctx = _handler(tmp_path)
    ctx.backend.apply_workspace_trust.side_effect = RuntimeError("rebuild failed")
    with pytest.raises(RuntimeError, match="rebuild failed"):
        await handler.methods()[RpcMethod.SET_WORKSPACE_TRUST]({"decision": "trust"})
    ctx.push.workspace_trust.assert_not_called()


async def test_repeating_a_decision_rebuilds_nothing_but_still_tells_the_windows(
    tmp_path: Path,
) -> None:
    handler, ctx = _handler(tmp_path)
    await handler.methods()[RpcMethod.SET_WORKSPACE_TRUST]({"decision": "trust"})
    again = await handler.methods()[RpcMethod.SET_WORKSPACE_TRUST]({"decision": "trust"})
    assert ctx.backend.apply_workspace_trust.await_count == 1
    assert ctx.push.workspace_trust.call_count == 2
    ctx.push.workspace_trust.assert_called_with(again)
