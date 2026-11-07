"""Pluggable transport layer for BE↔FE communication.

Provides an abstract Transport interface with implementations:
- InProcessTransport: asyncio.Queue pairs (single-process, tests)
- UnixSocketTransport: Unix domain socket (multi-process, production)
"""

from igni_core.transport.base import Transport
from igni_core.transport.in_process import InProcessTransport

__all__ = ["Transport", "InProcessTransport"]
