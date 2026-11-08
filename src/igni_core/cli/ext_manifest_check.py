"""What ``igni ext doctor`` checks in a manifest against the bundle it sits in.

Two kinds of finding, because they are not the same question. A *problem*
stops this working; a *note* is something to know. The distinction matters
more than it looks: the first version reported an unsigned development
bundle as a problem, so ``ext doctor`` failed on the output of ``ext init``
— teaching, as the very first thing an author sees, that the tooling
contradicts itself. Development packaging *requires* unsigned.
"""

from __future__ import annotations

from pathlib import Path

from igni_core.cli.ext_doctor import Findings
from igni_core.extensions.manifest import ExtensionManifest, Workspace
from igni_core.extensions.profile import DEV_FLAG


class ManifestCheck:
    """The manifest's packaging, grants and contributions, against its bundle."""

    def __init__(self, manifest: ExtensionManifest, bundle: Path) -> None:
        self._manifest = manifest
        self._bundle = bundle

    def check(self, findings: Findings) -> None:
        self._packaging(findings)
        self._pages(findings)
        self._endpoint(findings)
        self._placement(findings)
        self._dev_command(findings)
        self._wildcard_rpc(findings)

    def _packaging(self, findings: Findings) -> None:
        manifest = self._manifest
        if manifest.dev is not None:
            findings.notes.append(
                "development packaging: it runs only as a development install, "
                f"with {DEV_FLAG}=1. To reach anybody else, ship it from your "
                "service with 'python' packaging."
            )
        if manifest.python is not None:
            self._wheelhouse(manifest.python.wheels, findings)
        if manifest.sidecar is not None:
            findings.problems.append(
                f"packaged as a sidecar image ({manifest.sidecar.image}); igni "
                "cannot start container images yet. Use 'python', 'endpoint' or 'dev'."
            )

    def _wheelhouse(self, declared: str, findings: Findings) -> None:
        wheels = self._bundle / declared
        has_wheels = wheels.is_dir() and any(wheels.glob("*.whl"))
        buildable = (self._bundle / "pyproject.toml").is_file() or (
            self._bundle / "setup.py"
        ).is_file()
        if not has_wheels and not buildable:
            findings.problems.append(
                f"python packaging with no wheelhouse at {declared!r} "
                "and nothing to build one from (no pyproject.toml or setup.py)"
            )
        elif not has_wheels:
            findings.notes.append(
                f"no wheelhouse at {declared!r}: a development install "
                "builds one from this directory, network allowed. The bundle your "
                "service uploads must carry it, because a machine installs from it "
                "with no index."
            )

    def _pages(self, findings: Findings) -> None:
        if self._manifest.contributes.pages and not (self._bundle / "ui").is_dir():
            findings.problems.append(
                "contributes pages and the bundle has no 'ui' directory, "
                "so there is nothing to serve"
            )

    def _endpoint(self, findings: Findings) -> None:
        if self._manifest.endpoint is None:
            return
        permissions = self._manifest.permissions
        if not permissions.egress:
            findings.problems.append(
                "has an endpoint and declares no egress, so igni will refuse to dial it"
            )
        # igni sets an environment for a process it starts, and an endpoint is
        # somebody else's process. A grant that cannot be delivered is the
        # failure this system keeps removing.
        if permissions.workspace is not Workspace.NONE:
            findings.problems.append(
                f"declares workspace: {permissions.workspace.value} and "
                "is an endpoint, so the grant could never be delivered"
            )
        if permissions.secrets:
            findings.problems.append(
                f"declares secrets {list(permissions.secrets)} and is an "
                "endpoint, so they could never be delivered"
            )

    def _placement(self, findings: Findings) -> None:
        if self._manifest.runs_here:
            findings.notes.append(
                "runs as a process on the machine igni is on, so it installs only on "
                "a single-user backend. One process cannot serve a group: there is "
                "one workspace and one resolution of each secret, and no answer to "
                "whose."
            )
        if self._manifest.contributes.pages:
            findings.notes.append(
                "contributes pages, which only a single-user backend shows — a frame "
                "is hosted by the igni app beside the user."
            )

    def _dev_command(self, findings: Findings) -> None:
        if self._manifest.dev is None:
            return
        command = Path(self._manifest.dev.command[0])
        looks_local = command.parts and command.parts[0] in {".", ".."}
        if looks_local and not (self._bundle / command).exists():
            findings.problems.append(f"its dev command {command} is not in the bundle")

    def _wildcard_rpc(self, findings: Findings) -> None:
        # Reported rather than refused. A declared permission nobody enforces
        # used to be this system's defining bug, so a grant that is wider than
        # it looks is worth a line even when it is legal.
        if any(grant == "*" for grant in self._manifest.permissions.rpc):
            findings.problems.append("permissions.rpc contains '*', which grants every method")


__all__ = ["ManifestCheck"]
