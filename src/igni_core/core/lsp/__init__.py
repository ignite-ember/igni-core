"""Language Server Protocol plumbing — Claude Code plugin parity.

Plugins ship LSP server definitions in ``.lsp.json``; a session-
level :class:`LspServerManager` launches them on demand and routes
JSON-RPC requests through stdio. Agent-facing tools live in
``core.tools.lsp``.
"""

from igni_core.core.lsp.client import LspClient, LspClientError
from igni_core.core.lsp.loader import LspConfigLoader, load_lsp_config
from igni_core.core.lsp.manager import LspServerManager
from igni_core.core.lsp.schemas import (
    ClientCapabilities,
    InitializeParams,
    JsonRpcError,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    JsonRpcServerRequest,
    LspConfigFile,
    LspConfigLoadError,
    LspConfigLoadResult,
    LspServerConfig,
)

__all__ = [
    "ClientCapabilities",
    "InitializeParams",
    "JsonRpcError",
    "JsonRpcNotification",
    "JsonRpcRequest",
    "JsonRpcResponse",
    "JsonRpcServerRequest",
    "LspClient",
    "LspClientError",
    "LspConfigFile",
    "LspConfigLoadError",
    "LspConfigLoadResult",
    "LspConfigLoader",
    "LspServerConfig",
    "LspServerManager",
    "load_lsp_config",
]
