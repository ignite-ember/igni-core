"""Protocol messages for BE↔FE communication.

All messages are Pydantic models serializable to JSON.
The FE has zero Agno imports — all Agno-specific logic
stays in the BE's serializer.
"""

from igni_core.protocol.messages import *  # noqa: E402,F401,F403
