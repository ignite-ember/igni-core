"""Whether a folder is trusted to run its own code, and the one place that says so.

Every loader that would run something a folder brings — its
``.igni/tools/*.py``, its hooks, its workflows, its ``.lsp.json`` and
``.monitors.json`` commands, its plugins, a markdown command's
``!`cmd``` tokens — and the settings merge, which takes a folder's
``permissions`` and ``safety`` only from a trusted one, asks
:meth:`WorkspaceTrust.is_trusted` first. Reading files, ``igni.md`` and
rules does not need trust.

The answer, in order:

1. **Organisation policy** (``workspace_trust``, managed-only): ``never`` →
   untrusted; ``trusted_remotes`` with a remote that matches no pattern →
   untrusted, and the person cannot override it. With ``auto_trust_remotes``
   a matching remote is trusted without asking.
2. **This run**: "This time only", or ``--trust`` on a headless run — and
   "not this time", for a client that asks per run.
3. **The person's recorded decision** (:class:`WorkspaceTrustStore`).
4. **Unset** → ``ask``. Until someone answers, the folder runs untrusted.

The service is one object, handed to whoever needs it: the settings
loader hands it the organisation's policy each time settings load, and
the process's own instance is the one
:mod:`igni_core.core.workspace_trust.process` holds.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from igni_core.core.workspace_trust.decision import TrustDecision
from igni_core.core.workspace_trust.exceptions import TrustRefused
from igni_core.core.workspace_trust.identity import WorkspaceIdentity, remote_matches
from igni_core.core.workspace_trust.policy import WorkspaceTrustConfig
from igni_core.core.workspace_trust.run_trust import RunTrust
from igni_core.core.workspace_trust.store import TrustEntry, WorkspaceTrustStore

logger = logging.getLogger(__name__)


class WorkspaceTrust:
    """Decides, records and answers workspace trust.

    One instance per process — the one
    :mod:`igni_core.core.workspace_trust.process` holds — handed to every
    loader that gates a folder's own code.
    """

    def __init__(
        self,
        store: WorkspaceTrustStore | None = None,
        policy: WorkspaceTrustConfig | None = None,
    ) -> None:
        self.store = store or WorkspaceTrustStore()
        self._policy = policy
        #: The answers that last for this process only.
        self._run = RunTrust()
        #: The listeners told after a decision changes in this process.
        self._listeners: list[Callable[[TrustDecision], None]] = []
        #: The (folder, caller) pairs already logged as untrusted.
        self._logged: set[tuple[str, str]] = set()

    # ── policy ────────────────────────────────────────────────────

    @property
    def policy(self) -> WorkspaceTrustConfig:
        if self._policy is None:
            # No settings loaded yet in this process: the environment still
            # applies (the config's validator reads it).
            self._policy = WorkspaceTrustConfig()
        return self._policy

    def set_policy(self, policy: WorkspaceTrustConfig) -> None:
        self._policy = policy

    # ── deciding ──────────────────────────────────────────────────

    def decide(self, project_dir: str | Path) -> TrustDecision:
        """The answer for ``project_dir``, with the reason it was reached."""
        identity = WorkspaceIdentity.of(project_dir)
        mode = self.policy.mode

        decided = self._policy_decision(identity, mode)
        if decided is not None:
            return decided

        run = self._run.answer(identity.key)
        if run == "trusted":
            return TrustDecision(
                identity=identity,
                state="trusted",
                source="run",
                reason="Trusted for this run.",
                policy_mode=mode,
            )

        if run == "untrusted":
            return TrustDecision(
                identity=identity,
                state="untrusted",
                source="run",
                reason="You chose not to trust this folder for this run.",
                policy_mode=mode,
            )

        recorded = self.store.get(identity)
        if recorded is not None:
            return TrustDecision(
                identity=identity,
                state=recorded,
                source="store",
                reason=f"You chose to {'trust' if recorded == 'trusted' else 'not trust'} "
                "this folder.",
                policy_mode=mode,
            )
        return TrustDecision(
            identity=identity,
            state="ask",
            source="unset",
            reason="You have not decided whether to trust this folder yet.",
            policy_mode=mode,
        )

    def _policy_decision(self, identity: WorkspaceIdentity, mode: str) -> TrustDecision | None:
        """What the organisation's policy says, or ``None`` when it has no opinion."""
        if mode == "never":
            return TrustDecision(
                identity=identity,
                state="untrusted",
                source="policy",
                can_trust=False,
                reason="Your organisation's policy (workspace_trust.mode: never) "
                "does not allow any folder to be trusted.",
                policy_mode=mode,
            )
        if mode != "trusted_remotes":
            return None
        if not remote_matches(identity.remote, self.policy.trusted_remotes):
            why = (
                "this folder has no git remote"
                if not identity.remote
                else f"its git remote ({identity.remote}) is not one of them"
            )
            return TrustDecision(
                identity=identity,
                state="untrusted",
                source="policy",
                can_trust=False,
                reason="Your organisation only allows folders from approved git "
                f"remotes to be trusted, and {why}.",
                policy_mode=mode,
            )
        if self.policy.auto_trust_remotes:
            return TrustDecision(
                identity=identity,
                state="trusted",
                source="policy",
                reason="Trusted by your organisation's policy: its git remote is approved.",
                policy_mode=mode,
            )
        return None

    def is_trusted(self, project_dir: str | Path, *, what: str = "") -> bool:
        """True when ``project_dir`` may run its own code. ``what`` names the
        caller for a once-per-folder log line when the answer is no."""
        decision = self.decide(project_dir)
        if not decision.trusted and what:
            mark = (decision.identity.key, what)
            if mark not in self._logged:
                self._logged.add(mark)
                logger.info(
                    "Workspace %s is not trusted; skipping its %s. %s",
                    decision.identity.path,
                    what,
                    decision.reason,
                )
        return decision.trusted

    # ── answering ─────────────────────────────────────────────────

    def trust(self, project_dir: str | Path, *, remember: bool = True) -> TrustDecision:
        """Trust ``project_dir`` — for good (``remember``) or for this run.

        Raises :class:`TrustRefused` when the organisation's policy does not
        allow this folder to be trusted.
        """
        decision = self.decide(project_dir)
        if not decision.can_trust:
            raise TrustRefused(decision.reason)
        if remember:
            self.store.set(decision.identity, "trusted")
            self._run.forget(decision.identity.key)
        else:
            self._run.grant(decision.identity.key)
        return self._changed(project_dir)

    def distrust(self, project_dir: str | Path, *, remember: bool = True) -> TrustDecision:
        """Record "Don't trust" — for good (``remember``) or for this run."""
        identity = WorkspaceIdentity.of(project_dir)
        if remember:
            self.store.set(identity, "untrusted")
            self._run.forget(identity.key)
        else:
            self._run.refuse(identity.key)
        return self._changed(project_dir)

    def revoke(self, project_dir: str | Path) -> TrustDecision:
        """Forget every decision about ``project_dir``: it asks again."""
        identity = WorkspaceIdentity.of(project_dir)
        self._run.forget(identity.key)
        self.store.forget(identity)
        return self._changed(project_dir)

    def grant_for_run(self, project_dir: str | Path) -> TrustDecision:
        """``--trust``: trusted for this process if policy allows it.

        Unlike :meth:`trust`, never raises: a headless run passing
        ``--trust`` where policy forbids it stays untrusted and says so.
        """
        decision = self.decide(project_dir)
        if not decision.can_trust:
            logger.warning("--trust ignored for %s: %s", decision.identity.path, decision.reason)
            return decision
        self._run.grant(decision.identity.key)
        return self.decide(project_dir)

    def entries(self) -> list[TrustEntry]:
        return self.store.entries()

    # ── change notification ───────────────────────────────────────

    def subscribe(self, listener: Callable[[TrustDecision], None]) -> Callable[[], None]:
        """Call ``listener`` after a decision changes in this process."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def _changed(self, project_dir: str | Path) -> TrustDecision:
        decision = self.decide(project_dir)
        self._logged = {m for m in self._logged if m[0] != decision.identity.key}
        for listener in list(self._listeners):
            try:
                listener(decision)
            except Exception:  # noqa: BLE001 — a listener must not undo the decision
                logger.exception("workspace trust listener failed")
        return decision


__all__ = ["WorkspaceTrust"]
