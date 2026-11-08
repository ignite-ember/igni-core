"""Model registry / permission / update-check RPC handlers."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError

from pydantic import BaseModel, ConfigDict

from igni_core.backend.rpc_handlers.base import RpcHandler, WireArgs, rpc
from igni_core.backend.schemas_hitl import ToolCallArgs
from igni_core.backend.schemas_rpc import ModelRegistryResult, UpdateAvailable
from igni_core.core.config.schemas.display import DisplayConfig
from igni_core.core.config.tool_permissions import PermissionLevel
from igni_core.core.utils.update_checker import PackageMetadata, check_for_update
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod


class SwitchModelArgs(BaseModel):
    """``switch_model``: the model to make current."""

    model_config = ConfigDict(extra="ignore")

    model_name: str


class CheckPermissionArgs(BaseModel):
    """``check_permission``: one tool call to look up a level for."""

    model_config = ConfigDict(extra="ignore")

    tool_name: str
    func_name: str
    tool_args: ToolCallArgs


class SavePermissionRuleArgs(BaseModel):
    """``save_permission_rule``: a rule and the level it is saved under."""

    model_config = ConfigDict(extra="ignore")

    rule: str
    level: PermissionLevel


class ModelsRpcHandler(RpcHandler):
    """Model switching, verbose toggle, permission rule check + save,
    display-config + model-registry readers, and the update-check
    probe that surfaces "new version available" to the FE."""

    @rpc(RpcMethod.SWITCH_MODEL, args=SwitchModelArgs)
    def switch_model(self, args: SwitchModelArgs) -> msg.Info:
        reply = self._ctx.backend.switch_model(args.model_name)
        if isinstance(reply, msg.Error):
            # A refused switch fails the call, so write listeners
            # never announce a model that never became current.
            raise ValueError(reply.text)
        return reply

    @rpc(RpcMethod.TOGGLE_VERBOSE)
    def toggle_verbose(self, args: WireArgs) -> bool:
        return self._ctx.backend.toggle_verbose()

    @rpc(RpcMethod.CHECK_PERMISSION, args=CheckPermissionArgs)
    def check_permission(self, args: CheckPermissionArgs) -> PermissionLevel:
        return self._controllers.hitl.permissions.check(
            args.tool_name, args.func_name, args.tool_args
        )

    @rpc(RpcMethod.SAVE_PERMISSION_RULE, args=SavePermissionRuleArgs)
    def save_permission_rule(self, args: SavePermissionRuleArgs) -> None:
        self._controllers.hitl.permissions.save_rule(args.rule, args.level)

    @rpc(RpcMethod.GET_DISPLAY_CONFIG)
    def get_display_config(self, args: WireArgs) -> DisplayConfig:
        return self._ctx.backend.settings.display

    @rpc(RpcMethod.GET_MODEL_REGISTRY)
    def get_model_registry(self, args: WireArgs) -> ModelRegistryResult:
        return ModelRegistryResult.from_settings(self._ctx.backend.settings.models)

    @rpc(RpcMethod.CHECK_FOR_UPDATE)
    async def check_for_update(self, args: WireArgs) -> UpdateAvailable | None:
        """The newer release, ``None`` when there is none.

        A probe that could not reach an answer (network, HTTP status,
        unparseable reply) is refused with ``ValueError``, the way the
        other handlers refuse, so a caller can tell it from "no update".
        The FE's update check treats a refused call as nothing to show;
        a host extension gets a ``refused`` error.
        """
        info = await check_for_update()
        if info.error is not None:
            raise ValueError(f"update check failed ({info.error.value}): {info.error_detail}")
        if not info.available:
            return None
        try:
            package = PackageMetadata.load()
        except PackageNotFoundError as exc:
            raise ValueError(f"update check failed: no package metadata for {exc}") from None
        return UpdateAvailable.from_update_info(info, pkg_name=package.name)
