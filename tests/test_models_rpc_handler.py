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
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.backend.rpc_binding import RpcArgsError
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


def _probe_answers(monkeypatch: pytest.MonkeyPatch, answer: UpdateInfo) -> None:
    """Make the handler's update checker answer ``answer``."""
    checker = MagicMock()
    checker.check = AsyncMock(return_value=answer)
    monkeypatch.setattr(models_module, "UpdateChecker", MagicMock(return_value=checker))


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
    with pytest.raises(RpcArgsError, match="tool_name"):
        _call(handler, RpcMethod.CHECK_PERMISSION, {"func_name": "f", "tool_args": {}})


def test_save_permission_rule_saves_a_known_level() -> None:
    handler, ctx = _handler()
    _call(handler, RpcMethod.SAVE_PERMISSION_RULE, {"rule": "Bash(ls)", "level": "deny"})
    ctx.backend.controllers.hitl.permissions.save_rule.assert_called_once_with("Bash(ls)", "deny")


def test_save_permission_rule_refuses_an_unknown_level() -> None:
    handler, ctx = _handler()
    with pytest.raises(RpcArgsError, match="level"):
        _call(handler, RpcMethod.SAVE_PERMISSION_RULE, {"rule": "Bash(ls)", "level": "sometimes"})
    ctx.backend.controllers.hitl.permissions.save_rule.assert_not_called()


def test_a_refused_model_switch_fails_the_call() -> None:
    handler, ctx = _handler()
    ctx.backend.switch_model.return_value = msg.Error(text="no such model")
    with pytest.raises(ValueError, match="no such model"):
        _call(handler, RpcMethod.SWITCH_MODEL, {"model_name": "nope"})


async def test_no_newer_release_answers_none(monkeypatch: pytest.MonkeyPatch) -> None:
    handler, _ = _handler()
    _probe_answers(monkeypatch, UpdateInfo(available=False, current_version="1.0.0"))
    assert await _call(handler, RpcMethod.CHECK_FOR_UPDATE, {}) is None


async def test_a_newer_release_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    handler, _ = _handler()
    found = UpdateInfo(
        available=True,
        current_version="1.0.0",
        latest_version="1.1.0",
        download_url="https://example.invalid/igni",
    )
    _probe_answers(monkeypatch, found)
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
    _probe_answers(monkeypatch, failed)

    with pytest.raises(ValueError, match="network"):
        await _call(handler, RpcMethod.CHECK_FOR_UPDATE, {})


async def test_missing_package_metadata_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    handler, _ = _handler()
    found = UpdateInfo(available=True, current_version="1.0.0", latest_version="1.1.0")
    _probe_answers(monkeypatch, found)

    def _missing(cls: type[PackageMetadata]) -> PackageMetadata:
        raise PackageNotFoundError("igni-core")

    monkeypatch.setattr(models_module.PackageMetadata, "load", classmethod(_missing))

    with pytest.raises(ValueError, match="package metadata"):
        await _call(handler, RpcMethod.CHECK_FOR_UPDATE, {})


async def test_an_update_check_with_no_package_metadata_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A missing distribution must not reach the caller as a raw import error.

    The check reads the installed package's metadata before it can ask
    PyPI; a distribution that is not installed made that probe raise
    ``PackageNotFoundError`` past the update check's "never raises", so
    the call failed with a raw import error instead of the refusal every
    other failed check is. The checker now turns the failure into a
    typed error of its own, and the handler turns that into the refusal.
    """
    from igni_core.core.config.settings import Settings
    from igni_core.core.utils import update_checker

    def no_package() -> object:
        raise PackageNotFoundError("igni-core")

    # The checker is built by the handler, so the world it runs in is
    # set here: the check is on, the cache is empty, and the installed
    # distribution cannot be found.
    monkeypatch.setattr(
        update_checker.SettingsLoader,
        "load",
        classmethod(lambda cls: Settings(update_check_ttl=86400, air_gapped=False)),
    )
    monkeypatch.setattr(update_checker, "default_cache_path", lambda: tmp_path / ".update-check")
    monkeypatch.setattr(update_checker.PackageMetadata, "load", staticmethod(no_package))
    handler, _ = _handler()

    with pytest.raises(ValueError, match=r"update check failed \(unknown\)"):
        await _call(handler, RpcMethod.CHECK_FOR_UPDATE, {})


async def test_a_successful_model_switch_answers_an_rpc_response_with_its_declared_result() -> None:
    """Through the dispatcher: a client waiting for the ``rpc_response`` gets it.

    The success used to go out as an ``info`` message in place of one, so a
    caller that waits for the response timed out on a switch that worked.
    """
    from igni_core.backend.message_dispatcher import MessageDispatcher

    handler, ctx = _handler()
    ctx.backend.switch_model.return_value = msg.Info(text="Switched to m1")
    sent: list = []

    class _Transport:
        async def send(self, message) -> None:
            sent.append(message)

    dispatcher = MessageDispatcher(
        backend=None,  # type: ignore[arg-type]
        transport=_Transport(),
        rpc_table=handler.methods(),
        queue=[],
        login=None,
        extensions=None,
    )
    request = msg.RPCRequest(id="q", method="switch_model", args={"model_name": "m1"})

    await dispatcher.dispatch(request)

    (reply,) = sent
    assert isinstance(reply, msg.RPCResponse)
    assert reply.id == "q"
    assert reply.error is None
    assert reply.result == {"text": "Switched to m1"}
