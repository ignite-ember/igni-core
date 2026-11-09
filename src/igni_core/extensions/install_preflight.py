"""Whether this bundle may be installed here at all.

Everything decided before the extension is registered lives here: where
the bundle came from, what this deployment's shape forbids, and the
grants its packaging cannot deliver. A refusal raises
:class:`~igni_core.extensions.install_errors.InstallError` — the
bundle was never accepted, so there is no registry entry to record a
reason on.
"""

from __future__ import annotations

import logging

from igni_core.extensions.host.definition import HOST_SCHEMA
from igni_core.extensions.host.grants import HostGrants
from igni_core.extensions.install_context import InstallContext
from igni_core.extensions.install_errors import InstallError
from igni_core.extensions.manifest import ExtensionManifest, Workspace
from igni_core.extensions.profile import DEV_FLAG
from igni_core.extensions.store import DistributionRecord

logger = logging.getLogger(__name__)


class InstallPreflight:
    """The rules a bundle must pass before anything is registered."""

    def __init__(self, *, context: InstallContext) -> None:
        """
        Args:
            context: The deployment this install happens in — its
                profile, whether development installs are allowed, and
                where a declared secret's value comes from.
        """
        self._context = context

    def check(self, manifest: ExtensionManifest, distribution: DistributionRecord | None) -> None:
        """Every rule, in the order the install applies them.

        Raises:
            InstallError: The bundle is not installable here, and why.
        """
        self._check_trust(manifest, distribution)
        self._check_profile(manifest)
        self._check_deliverable(manifest)
        self._check_host_grants(manifest)
        self._check_tool_audience(manifest)

    def _check_trust(
        self, manifest: ExtensionManifest, distribution: DistributionRecord | None
    ) -> None:
        """Provenance, before anything is registered or started.

        Where the bundle came from decides, not what its manifest says
        about itself. A distributed one was verified against its
        service's key before it was stored, so here it only has to be
        the bundle that was verified, and not a ``dev`` command; its
        ``trust`` label is not consulted. Any other bundle is a
        development install, and needs the flag the person at this
        machine sets for themselves.
        """
        if distribution is not None:
            if (manifest.id, manifest.version) != (
                distribution.extension_id,
                distribution.version,
            ):
                raise InstallError(
                    f"{manifest.id} {manifest.version} is not what igni distributed "
                    f"({distribution.extension_id} {distribution.version}); the bundle "
                    "changed after it was verified. It is fetched again at the next sync."
                )
            if manifest.is_development_command:
                raise InstallError(
                    f"{manifest.id} uses 'dev' packaging, a command out of its "
                    "manifest, and igni does not run one that came from a service. "
                    "A service ships 'python' packaging."
                )
            logger.info(
                "extension %s %s distributed by igni for %s",
                manifest.id,
                manifest.version,
                distribution.service_url,
            )
            return

        if not self._context.allow_development:
            raise InstallError(
                f"{manifest.id} was not distributed by igni server, so it is a "
                f"development install, and {DEV_FLAG} is not set. Extensions reach "
                "this machine from their services through igni; set "
                f"{DEV_FLAG}=1 to run one you are developing."
            )
        logger.warning(
            "extension %s is a development install (trust %s); %s allows that",
            manifest.id,
            manifest.trust.value,
            DEV_FLAG,
        )

    def _check_profile(self, manifest: ExtensionManifest) -> None:
        """What this deployment's shape forbids.

        Both rules have been in EXTENSIBILITY since it was written
        and neither was checked, so an administrator could install the
        thing the document says does not work and find out from the
        behaviour.
        """
        if not self._context.profile.is_shared:
            return

        if manifest.runs_here:
            raise InstallError(
                f"{manifest.id} is packaged to run as a process here, and this "
                "backend serves more than one principal. One process would be "
                "one instance shared by all of them, with a single workspace "
                "and a single resolution of each secret — there is no answer "
                "to whose. Use 'endpoint' packaging, which is a service that "
                "can be multi-tenant."
            )

        if manifest.contributes.pages:
            raise InstallError(
                f"{manifest.id} contributes pages and this backend serves more "
                "than one principal. A frame is hosted by the igni app beside "
                "the user, and a shared backend has no such app — the pages "
                "would be served and never shown."
            )

    @staticmethod
    def _check_host_grants(manifest: ExtensionManifest) -> None:
        """Say which ``permissions.host`` entries name nothing the model has.

        Noted, never refused: a manifest written against a newer igni asks
        for objects this one does not have yet.
        ``workspace`` is named specifically, because it reads like a
        host grant and is decided by ``permissions.workspace``.
        """
        stray = HostGrants.unknown(list(manifest.permissions.host), HOST_SCHEMA.permission_names())
        if stray:
            logger.warning(
                "extension %s asks for host-model objects igni does not have: %s. "
                "host.workspace is granted by permissions.workspace, not here.",
                manifest.id,
                ", ".join(stray),
            )

    @staticmethod
    def _check_deliverable(manifest: ExtensionManifest) -> None:
        """Refuse a grant this packaging cannot deliver.

        ``workspace`` and ``secrets`` reach a service through its
        environment, and igni only sets an environment for a process
        it starts. A registered endpoint is somebody else's process:
        the grant is declared, approved by an administrator, and
        then silently does nothing — which is the failure this whole
        system has been removing, so it is refused rather than
        ignored.
        """
        if manifest.endpoint is None:
            return
        undeliverable = []
        if manifest.permissions.workspace is not Workspace.NONE:
            undeliverable.append(f"workspace: {manifest.permissions.workspace.value}")
        if manifest.permissions.secrets:
            undeliverable.append(f"secrets: {list(manifest.permissions.secrets)}")
        if not undeliverable:
            return
        raise InstallError(
            f"{manifest.id} declares {' and '.join(undeliverable)}, and igni "
            "delivers those in the environment of a process it starts. This is "
            "an endpoint — somebody else's process — so the grant would be "
            "approved and never arrive. Remove it, or have the service read "
            "its own configuration."
        )

    @staticmethod
    def _check_tool_audience(manifest: ExtensionManifest) -> None:
        """Refuse ``own-agents`` with no agents to be its own.

        The audience is "agents the carried plugin ships". With no
        plugin there are none, so every tool would be offered to
        nobody — approved at install and unreachable forever, the
        failure this contract keeps removing.
        """
        if manifest.permissions.toolAudience != "own-agents":
            return
        if not manifest.contributes.plugin:
            raise InstallError(
                f"{manifest.id} offers its tools to its own agents only "
                "(toolAudience: own-agents) and carries no plugin, so it "
                "has no agents of its own. Ship them with "
                "contributes.plugin, or use toolAudience: all-agents."
            )
