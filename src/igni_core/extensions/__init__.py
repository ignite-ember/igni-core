"""The extension system.

An extension is one concept with two packagings — a signed sidecar
image we supervise, or a remote endpoint somebody else runs. Tools
always travel over MCP; a UI is an optional contribution. See
``docs/EXTENSIBILITY.md`` for the contract and ``docs/BUILD_PLAN.md``
for what is built and in what order.
"""

from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.origin import ExtensionOrigin, ExtensionOrigins
from igni_core.extensions.profile import DeploymentProfile
from igni_core.extensions.registry import (
    ExtensionMethod,
    ExtensionRefused,
    ExtensionRegistry,
    ExtensionState,
    InstalledExtension,
)
from igni_core.extensions.rpc import ExtensionRpc
from igni_core.extensions.service import (
    ExtensionService,
    ExtensionServiceError,
    McpExtensionService,
)

__all__ = [
    "ExtensionManifest",
    "ExtensionMethod",
    "ExtensionOrigin",
    "ExtensionOrigins",
    "DeploymentProfile",
    "ExtensionRefused",
    "ExtensionRegistry",
    "ExtensionRpc",
    "ExtensionService",
    "ExtensionServiceError",
    "ExtensionState",
    "InstalledExtension",
    "McpExtensionService",
]
