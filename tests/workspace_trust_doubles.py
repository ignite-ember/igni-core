"""The trust service the suite uses, and the one seam it opens.

Workspace trust (:mod:`igni_core.core.workspace_trust`) starts every folder
undecided, and an undecided folder runs untrusted: its tools, hooks,
workflows and project settings do nothing until the person says otherwise.
Most of the suite is about other things and opens project folders as a
trusted one would be, so it needs a trust service that says yes.

The seam is one class — :class:`TrustEverything` — and it changes one
answer only: a folder nobody has decided about is trusted. Policy, this
run's answers and the person's recorded decisions still say what they say,
so a test about trust gets the real service and the real decision, and a
test about something else gets a folder that is trusted unless something
else has an opinion.

The old production flag this replaces (``WorkspaceTrust(trust_everything=True)``)
sat in the service itself, where a caller could reach it at runtime. Here it
is a subclass in the test suite, where only tests can find it.
"""

from __future__ import annotations

from pathlib import Path

from igni_core.core.workspace_trust import TrustDecision, WorkspaceTrust


class TrustEverything(WorkspaceTrust):
    """A trust service that trusts every folder nobody has decided about."""

    def decide(self, project_dir: str | Path) -> TrustDecision:
        """The answer for ``project_dir``, with an undecided folder trusted."""
        decision = super().decide(project_dir)
        if decision.source != "unset":
            return decision
        return decision.model_copy(
            update={
                "state": "trusted",
                "source": "run",
                "reason": "The test suite trusts this folder.",
            }
        )


__all__ = ["TrustEverything"]
