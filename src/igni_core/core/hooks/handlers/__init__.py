"""Hook handlers — one class per hook type + a registry."""

from igni_core.core.hooks.handlers.base import HookHandler
from igni_core.core.hooks.handlers.command import CommandHookHandler
from igni_core.core.hooks.handlers.http import HttpHookHandler
from igni_core.core.hooks.handlers.mcp_tool import (
    McpInvocationArgs,
    MCPResolver,
    McpToolHookHandler,
)
from igni_core.core.hooks.handlers.prompt import PromptHookHandler
from igni_core.core.hooks.handlers.registry import HookHandlerRegistry

__all__ = [
    "HookHandler",
    "CommandHookHandler",
    "HttpHookHandler",
    "PromptHookHandler",
    "McpToolHookHandler",
    "McpInvocationArgs",
    "MCPResolver",
    "HookHandlerRegistry",
]
