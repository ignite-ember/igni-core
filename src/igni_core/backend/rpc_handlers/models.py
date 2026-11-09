"""Model registry / permission / update-check RPC handlers."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError

from pydantic import BaseModel, ConfigDict

from igni_core.backend.rpc_binding import NoArgs, rpc
from igni_core.backend.rpc_handlers.base import RpcHandler
from igni_core.backend.schemas_hitl import ToolCallArgs
from igni_core.backend.schemas_rpc import ModelRegistryResult, UpdateAvailable
from igni_core.core.config.schemas.display import DisplayConfig
from igni_core.core.config.tool_permissions import PermissionLevel
from igni_core.core.utils.update_checker import PackageMetadata, check_for_update
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod, RpcScope


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

    @rpc(RpcMethod.SWITCH_MODEL, args=SwitchModelArgs, scope=RpcScope.SESSION)
    def switch_model(self, args: SwitchModelArgs) -> msg.Info:
        """Make another model the one this session's agent runs on.

        ``model_name`` is a name from the model registry
        (``get_model_registry``). The choice is also saved as the default
        for new sessions. Returns an ``info`` message, sent in place of an
        ``rpc_response``, saying the model switched, with a note for any
        part of the choice that could not be saved. Fails when the model is
        not in the registry.
        """
        reply = self._ctx.backend.switch_model(args.model_name)
        if isinstance(reply, msg.Error):
            # A refused switch fails the call, so write listeners
            # never announce a model that never became current.
            raise ValueError(reply.text)
        return reply

    @rpc(RpcMethod.TOGGLE_VERBOSE, args=NoArgs, scope=RpcScope.SESSION)
    def toggle_verbose(self, args: NoArgs) -> bool:
        """Turn showing the agent's routing decisions on or off for this session.

        Takes no arguments. Returns the new setting: ``true`` when routing is
        now shown. Not saved; the session's next settings reload resets it.
        """
        return self._ctx.backend.toggle_verbose()

    @rpc(RpcMethod.CHECK_PERMISSION, args=CheckPermissionArgs, scope=RpcScope.SESSION)
    def check_permission(self, args: CheckPermissionArgs) -> PermissionLevel:
        """Look up the saved permission level for one tool call.

        ``tool_name`` and ``func_name`` name the tool and its function;
        ``tool_args`` are the call's arguments, which argument rules match
        against. The rules are read from this session's project and the
        person's settings files. Returns ``allow``, ``ask`` or ``deny``:
        the last matching argument rule, else the tool's own level, else
        ``ask``. The session's permission mode is not applied.
        """
        return self._controllers.hitl.permissions.check(
            args.tool_name, args.func_name, args.tool_args
        )

    @rpc(RpcMethod.SAVE_PERMISSION_RULE, args=SavePermissionRuleArgs, scope=RpcScope.SESSION)
    def save_permission_rule(self, args: SavePermissionRuleArgs) -> None:
        """Save a permission rule for this session's project.

        ``rule`` is a permission rule (``Bash(git status)``, a tool name);
        ``level`` is ``allow``, ``ask`` or ``deny``. The rule is written to
        the project's ``.igni/settings.local.json`` (the person's own when
        there is no project), and the session follows it from its next tool
        call. Returns nothing. Fails when the settings file cannot be
        written.
        """
        self._controllers.hitl.permissions.save_rule(args.rule, args.level)

    @rpc(RpcMethod.GET_DISPLAY_CONFIG, args=NoArgs, scope=RpcScope.SESSION)
    def get_display_config(self, args: NoArgs) -> DisplayConfig:
        """Read this session's display settings.

        Takes no arguments. Returns how output is shown: markdown rendering,
        tool calls, routing, reasoning, the colour theme, and how many lines
        of a tool result and a message are shown before truncating.
        """
        return self._ctx.backend.settings.display

    @rpc(RpcMethod.GET_MODEL_REGISTRY, args=NoArgs, scope=RpcScope.SESSION)
    def get_model_registry(self, args: NoArgs) -> ModelRegistryResult:
        """List the models this session can run on.

        Takes no arguments. Returns the ``default`` model's name, the
        largest context window configured, and ``registry``: every model by
        name, with its provider and settings.
        """
        return ModelRegistryResult.from_settings(self._ctx.backend.settings.models)

    @rpc(RpcMethod.CHECK_FOR_UPDATE, args=NoArgs, scope=RpcScope.IGNI)
    async def check_for_update(self, args: NoArgs) -> UpdateAvailable | None:
        """Ask whether a newer igni release is available.

        Takes no arguments. Returns the newer release — its version, the
        version installed, where to get it — or ``null`` when there is none
        (or update checks are switched off). Fails when the check could not
        reach an answer (network, HTTP status, an unreadable reply), so a
        caller can tell that from "no update".
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
