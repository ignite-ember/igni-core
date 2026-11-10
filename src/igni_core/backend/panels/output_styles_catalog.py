"""Output-styles catalog controller.

Owns the output-styles picker chip: discovered styles + the
currently-applied one, read through the session's
:class:`~igni_core.core.session.output_style_selection.OutputStyleSelection`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from igni_core.backend.schemas_panels import OutputStyleInfo, OutputStylesResult

if TYPE_CHECKING:
    from igni_core.core.session import Session


class OutputStylesCatalog:
    """Snapshot of discovered output styles for the picker chip."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def snapshot(self) -> OutputStylesResult:
        """Discovered styles + the currently-active one."""
        selection = self._session.output_style
        styles = selection.styles
        active = selection.active or ""
        return OutputStylesResult(
            active=active,
            styles=[
                OutputStyleInfo(name=s.name, description=s.description)
                for s in sorted(styles.values(), key=lambda s: s.name)
            ],
        )
