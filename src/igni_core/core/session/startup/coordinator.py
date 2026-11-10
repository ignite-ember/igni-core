"""Session-startup coordinator.

Thin composer — owns one instance of each of the warmup
phases and exposes the same public method names Session /
backend already call. Every method is a one-line delegate so
the file stays a single-concern composition point rather than a
300-line implementation.

The phases themselves live in sibling modules
(:mod:`~.marketplace`, :mod:`~.mcp`); each is a subclass of
:class:`~.base.SessionStartupPhase` so shared loop-scheduling
and log-swallowing behaviour lives in one place.
"""

from __future__ import annotations

from igni_core.core.session.schemas import McpInitResult
from igni_core.core.session.startup.marketplace import MarketplaceWarmupPhase
from igni_core.core.session.startup.mcp import McpInitPhase


class SessionStartupCoordinator:
    """Composes the session-startup phases behind Session's
    boot-time public surface.

    Both phases are injected, already built from the collaborators they
    read; ``self.mcp`` is the sole owner of the once-per-session
    ``_initialized`` flag (Rule AP3 fixed — one write site).
    """

    def __init__(self, marketplace: MarketplaceWarmupPhase, mcp: McpInitPhase) -> None:
        self.marketplace = marketplace
        self.mcp = mcp

    # ── MCP init flag proxies (Session compat) ────────────────

    @property
    def mcp_initialized(self) -> bool:
        """Route to :attr:`McpInitPhase.initialized` — the single owner."""
        return self.mcp.initialized

    @mcp_initialized.setter
    def mcp_initialized(self, value: bool) -> None:
        self.mcp.initialized = value

    # ── Marketplace warmup ───────────────────────────────────

    def start_marketplace_refresh_background(self) -> None:
        """See :meth:`MarketplaceWarmupPhase.start_background`."""
        self.marketplace.start_background()

    # ── MCP first-connect + rebuild ───────────────────────────

    async def ensure_mcp(self) -> McpInitResult:
        """See :meth:`McpInitPhase.ensure`.

        Returns a Pattern-3 :class:`McpInitResult` envelope so
        callers can branch on ``connected`` / ``failed`` /
        ``rebuilt`` / ``skipped_reason`` without log-scraping."""
        return await self.mcp.ensure()

    def rebuild_mcp(self) -> None:
        """See :meth:`McpInitPhase.rebuild_current`."""
        self.mcp.rebuild_current()
