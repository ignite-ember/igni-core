"""The model / permission / update-check RPCs validate their arguments
and say when the update probe failed.

``save_permission_rule`` used to pass the wire's ``level`` straight to
the settings writer, so ``"sometimes"`` landed in
``permissions.sometimes`` where no reader looks. The arguments now
validate first. ``check_for_update`` used to answer ``None`` for a
failed probe as well as for "nothing newer"; a failure is now refused
with ``ValueError``, which the host gateway maps to ``refused``.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from igni_core.backend.rpc_handlers import models as models_module
from igni_core.backend.rpc_handlers.models import ModelsRpcHandler
from igni_core.backend.schemas_hitl import ToolCallArgs
from igni_core.core.utils.update_checker import PackageMetadata, UpdateError, UpdateInfo
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod


def _handler() -> tuple[ModelsRpcHandler, MagicMock]:
    ctx = MagicMock()
    return ModelsRpcHandler(ctx), ctx


def _call(handler: ModelsRpcHandler, method: RpcMethod, wire: dict):
    return handler.methods()[method](wire)


def test_check_permission_gets_typed_tool_args() -> None:
    handler, ctx = _handler()
    permissions = ctx.backend.controllers.hitl.permissions
    permissions.check.return_value = "allow"

    level = _call(
        handler,
        RpcMethod.CHECK_PERMISSION,
        {"tool_name": "Bash", "func_name": "run_shell_command", "tool_args": {"command": "ls"}},
    )

    assert level == "allow"
    tool_name, func_name, tool_args = permissions.check.call_args.args
    assert (tool_name, func_name) == ("Bash", "run_shell_command")
    assert isinstance(tool_args, ToolCallArgs)
    assert tool_args.command == "ls"


def test_check_permission_without_tool_name_is_refused() -> None:
    handler, _ = _handler()
    with pytest.raises(ValidationError, match="tool_name"):
        _call(handler, RpcMethod.CHECK_PERMISSION, {"func_name": "f", "tool_args": {}})


def test_save_permission_rule_saves_a_known_level() -> None:
    handler, ctx = _handler()
    _call(handler, RpcMethod.SAVE_PERMISSION_RULE, {"rule": "Bash(ls)", "level": "deny"})
    ctx.backend.controllers.hitl.permissions.save_rule.assert_called_once_with("Bash(ls)", "deny")


def test_save_permission_rule_refuses_an_unknown_level() -> None:
    handler, ctx = _handler()
    with pytest.raises(ValidationError, match="level"):
        _call(handler, RpcMethod.SAVE_PERMISSION_RULE, {"rule": "Bash(ls)", "level": "sometimes"})
    ctx.backend.controllers.hitl.permissions.save_rule.assert_not_called()


def test_a_refused_model_switch_fails_the_call() -> None:
    handler, ctx = _handler()
    ctx.backend.switch_model.return_value = msg.Error(text="no such model")
    with pytest.raises(ValueError, match="no such model"):
        _call(handler, RpcMethod.SWITCH_MODEL, {"model_name": "nope"})


async def test_no_newer_release_answers_none(monkeypatch: pytest.MonkeyPatch) -> None:
    handler, _ = _handler()
    monkeypatch.setattr(
        models_module,
        "check_for_update",
        AsyncMock(return_value=UpdateInfo(available=False, current_version="1.0.0")),
    )
    assert await _call(handler, RpcMethod.CHECK_FOR_UPDATE, {}) is None


async def test_a_newer_release_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    handler, _ = _handler()
    found = UpdateInfo(
        available=True,
        current_version="1.0.0",
        latest_version="1.1.0",
        download_url="https://example.invalid/igni",
    )
    monkeypatch.setattr(models_module, "check_for_update", AsyncMock(return_value=found))
    monkeypatch.setattr(
        models_module.PackageMetadata, "load", classmethod(lambda cls: PackageMetadata(name="igni"))
    )

    update = await _call(handler, RpcMethod.CHECK_FOR_UPDATE, {})

    assert (update.latest_version, update.pkg_name) == ("1.1.0", "igni")


async def test_a_failed_probe_fails_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    handler, _ = _handler()
    failed = UpdateInfo(
        available=False,
        current_version="1.0.0",
        error=UpdateError.NETWORK,
        error_detail="connection refused",
    )
    monkeypatch.setattr(models_module, "check_for_update", AsyncMock(return_value=failed))

    with pytest.raises(ValueError, match="network"):
        await _call(handler, RpcMethod.CHECK_FOR_UPDATE, {})


async def test_missing_package_metadata_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    handler, _ = _handler()
    found = UpdateInfo(available=True, current_version="1.0.0", latest_version="1.1.0")
    monkeypatch.setattr(models_module, "check_for_update", AsyncMock(return_value=found))

    def _missing(cls: type[PackageMetadata]) -> PackageMetadata:
        raise PackageNotFoundError("igni-core")

    monkeypatch.setattr(models_module.PackageMetadata, "load", classmethod(_missing))

    with pytest.raises(ValueError, match="package metadata"):
        await _call(handler, RpcMethod.CHECK_FOR_UPDATE, {})
