"""Protocol messages for BE↔FE communication.

All messages are Pydantic models serializable to JSON.
The FE has zero Agno imports — all Agno-specific logic
stays in the BE's serializer.
"""

from igni_core.protocol.agno_compat import AgnoCompatibilityShim
from igni_core.protocol.messages import *  # noqa: E402,F401,F403

# Install the Agno TeamRunOutput.agent_* shim at protocol-package
# import time. This is the earliest point in the import graph
# where any code path that eventually creates a team run has
# already touched something under ``igni_core.protocol``; deferring
# to backend startup would leak the un-patched class into tests
# that import protocol messages without booting the backend.
# The shim itself is idempotent — see :class:`AgnoCompatibilityShim`.
AgnoCompatibilityShim.apply()
