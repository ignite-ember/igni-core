"""Starting an extension's process inside an OS sandbox: the one interface.

:meth:`ExtensionSandbox.confine` takes the stdio MCP server config the
installer built and returns one that runs the same command confined to
the extension's grants (:class:`~.plan.SandboxPlan`), with an egress
proxy (:class:`~.proxy.EgressProxy`) running for it in igni. Every
process igni starts for an extension goes through here
(:meth:`~igni_core.extensions.install_process.ExtensionProcess.confine`
confines the config the installer built, before it is started); an
``endpoint`` extension has no process here and is left as it is.

What the process sees changes in four ways, all in its environment:

* ``HTTP_PROXY``/``HTTPS_PROXY``/``ALL_PROXY`` (and lower case) name the
  proxy, with the run's credentials in the URL; ``NO_PROXY`` keeps
  loopback direct, where igni's host channel,
  token endpoint and the extension's own local database are.
* ``HOME`` is a directory of its run's own, and ``TMPDIR`` (``TMP``,
  ``TEMP``, Java's ``java.io.tmpdir`` through ``JAVA_TOOL_OPTIONS``) its
  run's temp directory — the person's home is not readable, and a tool
  that finds its config there would fail rather than start empty.
* ``IGNI_SANDBOX`` names the sandbox (``seatbelt``, ``bubblewrap``).

Which sandbox that is depends on the machine: macOS uses Seatbelt
(:mod:`.seatbelt`), Linux bubblewrap (:mod:`.bubblewrap`), and Windows
nothing yet. The two are asked the same questions through one interface
(:mod:`.mechanism`), so this module names neither. Where there is no
sandbox the process runs as before, with a warning in the log and on the
extension (:attr:`Confinement.reason`), unless
``extensions.require_sandbox`` is set, which refuses it
(:class:`SandboxRefused`). ``extensions.sandbox: false`` turns all of
this off for development (:class:`~.settings.SandboxSettings`).
"""

from __future__ import annotations

import logging
import secrets
import shutil
import sys
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from igni_core.core.config.group_policy.settings_fetcher import CachedSettingsFetcher
from igni_core.core.config.settings import Settings
from igni_core.core.mcp.config import MCPServerConfig, MCPTransport
from igni_core.core.network_policy import NetworkPolicy
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.permissions import PermissionDenied
from igni_core.extensions.sandbox.mechanism import (
    MechanismName,
    SandboxAvailability,
    SandboxMechanism,
    probe_platform,
)
from igni_core.extensions.sandbox.plan import SandboxPlan
from igni_core.extensions.sandbox.proxy import (
    Connector,
    EgressProxy,
    LoopbackRelay,
    stop_relays,
)
from igni_core.extensions.sandbox.settings import SandboxSettings

logger = logging.getLogger(__name__)

#: Told to the process: which sandbox it runs in.
SANDBOX_ENV = "IGNI_SANDBOX"

#: Where the proxy is not used: this machine.
#: Not ``.localhost``: the sandbox has no DNS, so such a name is reached
#: through the proxy, which resolves it outside (RFC 6761: to loopback).
NO_PROXY = "localhost,127.0.0.1,::1"


class SandboxRefused(PermissionDenied):
    """``extensions.require_sandbox`` is set and this machine has no sandbox."""


class Confinement(BaseModel):
    """How one extension's process runs: confined, or not and why."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    extension_id: str
    #: Which sandbox confines it; ``None`` when it runs unconfined.
    mechanism: MechanismName | None = None
    #: Why it runs unconfined; empty when confined.
    reason: str = ""
    #: Unconfined because a setting turned the sandbox off, rather than
    #: because this machine has none.
    deliberate: bool = False
    #: The grants it runs with.
    plan: SandboxPlan | None = None
    #: The egress proxy igni runs for it.
    proxy: EgressProxy | None = None
    #: The relays that bridge igni's loopback ports into its sandbox.
    relays: list[LoopbackRelay] = Field(default_factory=list)

    @property
    def confined(self) -> bool:
        return self.mechanism is not None

    def describe(self) -> str:
        """One line for the extensions panel."""
        if self.confined:
            return f"sandboxed ({self.mechanism})"
        return f"not sandboxed: {self.reason}"


class ExtensionSandbox:
    """Wraps extension commands in the platform's sandbox, and runs their proxies."""

    def __init__(
        self,
        *,
        settings: SandboxSettings | None = None,
        network: NetworkPolicy | None = None,
        platform: str | None = None,
        connect: Connector | None = None,
        run_root: Path | None = None,
    ) -> None:
        """
        Args:
            settings: The two switches; read from managed, env and user
                settings by default (never a project's).
            network: The air gap. Without one, nothing is narrowed.
            platform: ``sys.platform`` by default; tests pass another to
                simulate a machine with no sandbox.
            connect: How the proxy opens an upstream connection (tests).
            run_root: Where run directories go; the system temp dir.
        """
        self._settings = settings if settings is not None else SandboxSettings.load()
        self._network = network or NetworkPolicy()
        self._platform = platform or sys.platform
        self._connect = connect
        self._run_root = run_root
        self._availability: SandboxAvailability | None = None
        self._runs: dict[str, Confinement] = {}

    @classmethod
    def for_settings(cls, settings: Settings) -> ExtensionSandbox:
        """From igni's loaded settings: its air gap, and the switches from their tiers.

        The organisation's group policy counts for ``require_sandbox``
        only as the pack igni server signed and igni verifies here, the
        same way the settings loader reads it. A pack that cannot be
        read is not the same as no pack, so nothing here hides a failure
        to read it: it is raised, rather than running the extensions
        without the organisation's ``require_sandbox``.
        """
        return cls(
            settings=SandboxSettings.load(group=CachedSettingsFetcher.for_settings(settings)),
            network=NetworkPolicy.from_settings(settings),
        )

    @property
    def settings(self) -> SandboxSettings:
        return self._settings

    def status(self, extension_id: str) -> Confinement | None:
        return self._runs.get(extension_id)

    async def availability(self) -> SandboxAvailability:
        """Whether this machine has a sandbox, and which one."""
        if self._availability is None:
            self._availability = await probe_platform(self._platform)
        return self._availability

    # ── the one entry point ────────────────────────────────────────

    async def confine(
        self,
        manifest: ExtensionManifest,
        config: MCPServerConfig,
        *,
        bundle: Path,
        data_dir: Path | None,
        project_dir: Path | None,
        service_url: str | None,
        development: bool,
    ) -> MCPServerConfig:
        """``config`` as it runs in the sandbox, or as it is where it cannot.

        Raises:
            SandboxRefused: No sandbox here and ``require_sandbox`` is set.
        """
        if config.type is not MCPTransport.stdio or not config.command:
            return config
        await self.release(manifest.id)

        if not self._settings.enabled:
            self._runs[manifest.id] = Confinement(
                extension_id=manifest.id,
                reason=f"extensions.sandbox is false in the {self._settings.disabled_by} settings",
                deliberate=True,
            )
            logger.warning(
                "extension %s runs UNSANDBOXED: extensions.sandbox is false (%s settings)",
                manifest.id,
                self._settings.disabled_by,
            )
            return config

        availability = await self.availability()
        mechanism = availability.mechanism
        if mechanism is None:
            if self._settings.required:
                raise SandboxRefused(
                    f"{manifest.id} was not started: extensions.require_sandbox is set "
                    f"and this machine has no sandbox ({availability.reason})."
                )
            self._runs[manifest.id] = Confinement(
                extension_id=manifest.id, reason=availability.reason
            )
            logger.warning(
                "extension %s runs UNSANDBOXED, with this person's full file and network "
                "access: %s. Set extensions.require_sandbox in managed settings to refuse "
                "instead.",
                manifest.id,
                availability.reason,
            )
            return config

        run_dir = Path(
            tempfile.mkdtemp(prefix=f"igni-ext-{_safe(manifest.id)}-", dir=self._run_root)
        ).resolve()
        try:
            return await self._wrap(
                mechanism,
                manifest,
                config,
                run_dir=run_dir,
                bundle=bundle,
                data_dir=data_dir,
                project_dir=project_dir,
                service_url=service_url,
                development=development,
            )
        except Exception:
            await self.release(manifest.id)
            _remove_run_dir(run_dir)
            raise

    async def _wrap(
        self,
        mechanism: SandboxMechanism,
        manifest: ExtensionManifest,
        config: MCPServerConfig,
        *,
        run_dir: Path,
        bundle: Path,
        data_dir: Path | None,
        project_dir: Path | None,
        service_url: str | None,
        development: bool,
    ) -> MCPServerConfig:
        env = dict(config.env or {})
        cwd = Path(config.cwd) if config.cwd else None
        plan = SandboxPlan.build(
            manifest,
            command=config.command or "",
            env=env,
            cwd=cwd,
            bundle=bundle,
            data_dir=data_dir,
            project_dir=project_dir,
            service_url=service_url,
            development=development,
            network=self._network,
            run_dir=run_dir,
        )
        plan.home_dir.mkdir(parents=True, exist_ok=True)
        if plan.withheld:
            logger.warning(
                "extension %s: %s withheld from its sandbox — outside this air-gapped network",
                manifest.id,
                ", ".join(plan.withheld),
            )

        if plan.plain_http_service:
            logger.warning(
                "extension %s: its service %s is plain http, so its process may not reach "
                "it. Serve the service over https, or, developing on this machine, start "
                "igni with IGNI_DEV_EXTENSIONS=1.",
                manifest.id,
                plan.plain_http_service,
            )

        proxy = EgressProxy(
            manifest.id, plan.hosts, connect=self._connect, token=secrets.token_urlsafe(24)
        )
        run = Confinement(
            extension_id=manifest.id, mechanism=mechanism.name, plan=plan, proxy=proxy
        )
        self._runs[manifest.id] = run

        prepared = await mechanism.prepare(
            plan,
            proxy,
            command=config.command or "",
            args=list(config.args or []),
            cwd=cwd,
        )
        run.relays.extend(prepared.relays)

        env.update(self._environment(plan, proxy.url(prepared.proxy_port), mechanism.name, env))
        logger.info(
            "extension %s sandboxed (%s): reads %d paths, writes %s, reaches %s",
            manifest.id,
            mechanism.name,
            len(plan.read),
            ", ".join(str(p) for p in plan.write),
            ", ".join(plan.hosts.entries) or "no host beyond this machine",
        )
        return config.model_copy(
            update={"command": prepared.command, "args": prepared.args, "env": env}
        )

    @staticmethod
    def _environment(
        plan: SandboxPlan, proxy: str, mechanism: MechanismName, env: dict[str, str]
    ) -> dict[str, str]:
        tmp = str(plan.temp_dir)
        java = env.get("JAVA_TOOL_OPTIONS", "")
        out = {
            SANDBOX_ENV: mechanism.value,
            "HOME": str(plan.home_dir),
            "TMPDIR": tmp,
            "TMP": tmp,
            "TEMP": tmp,
            "JAVA_TOOL_OPTIONS": f"{java} -Djava.io.tmpdir={tmp}".strip(),
        }
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            out[name] = proxy
            out[name.lower()] = proxy
        out["NO_PROXY"] = out["no_proxy"] = NO_PROXY
        return out

    async def release(self, extension_id: str) -> None:
        """Stop its proxy and relays and remove its run directory."""
        run = self._runs.pop(extension_id, None)
        if run is None:
            return
        if run.proxy is not None:
            try:
                await run.proxy.stop()
            except Exception as exc:  # noqa: BLE001 — teardown must not raise
                logger.warning("the egress proxy for %s did not stop: %s", extension_id, exc)
        await stop_relays(run.relays)
        if run.plan is not None and run.plan.run_dir is not None:
            _remove_run_dir(run.plan.run_dir)

    async def close(self) -> None:
        for extension_id in list(self._runs):
            await self.release(extension_id)


def _safe(extension_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in extension_id)[:40]


def _remove_run_dir(run_dir: Path) -> None:
    """Remove a run directory, saying so when part of it stays.

    Best effort: a run directory that will not go must not stop the
    extension from being released, but it is said out loud, because it
    holds what the extension wrote.
    """
    try:
        shutil.rmtree(run_dir)
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("the run directory %s was not removed: %s", run_dir, exc)


__all__ = ["NO_PROXY", "SANDBOX_ENV", "Confinement", "ExtensionSandbox", "SandboxRefused"]
