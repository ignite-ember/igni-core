"""Workspace trust: opening a folder never runs its code until the person trusts it.

* :mod:`.identity` — a folder's canonical path and git remote, the store's key.
* :mod:`.store` — the person's decisions, in ``~/.igni/workspace-trust.json``.
* :mod:`.decision` — the answer for one folder, with the reason it was reached.
* :mod:`.run_trust` — the answers that last for this process only.
* :mod:`.policy` — the organisation's ``workspace_trust`` settings block.
* :mod:`.service` — :class:`WorkspaceTrust`, which every gated loader asks.
* :mod:`.process` — the instance this process uses, and :func:`process_trust`.
* :mod:`.gate` — the service a consumer asks, given one or the process's.

Organisation policy is ``workspace_trust`` in settings
(:class:`~igni_core.core.workspace_trust.policy.WorkspaceTrustConfig`),
honoured only from the managed file, the group policy pack and the environment.
"""

from igni_core.core.workspace_trust.decision import (
    GATED_SUMMARY,
    DecisionSource,
    DecisionState,
    TrustDecision,
)
from igni_core.core.workspace_trust.exceptions import TrustRefused
from igni_core.core.workspace_trust.gate import TrustGate
from igni_core.core.workspace_trust.identity import (
    WorkspaceIdentity,
    canonical_remote,
    remote_matches,
)
from igni_core.core.workspace_trust.policy import WorkspaceTrustConfig, WorkspaceTrustMode
from igni_core.core.workspace_trust.process import (
    TrustServiceHolder,
    process_trust,
    trust_service,
)
from igni_core.core.workspace_trust.run_trust import RunAnswer, RunTrust
from igni_core.core.workspace_trust.service import WorkspaceTrust
from igni_core.core.workspace_trust.store import TrustEntry, WorkspaceTrustStore

__all__ = [
    "GATED_SUMMARY",
    "DecisionSource",
    "DecisionState",
    "RunAnswer",
    "RunTrust",
    "TrustDecision",
    "TrustEntry",
    "TrustGate",
    "TrustRefused",
    "TrustServiceHolder",
    "WorkspaceIdentity",
    "WorkspaceTrust",
    "WorkspaceTrustConfig",
    "WorkspaceTrustMode",
    "WorkspaceTrustStore",
    "canonical_remote",
    "process_trust",
    "remote_matches",
    "trust_service",
]
