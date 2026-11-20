"""The cloud layer: igni in a producer's microVM (``IGNI_PROFILE=cloud``).

``docs/CLOUD.md`` §9. Declared as the ``cloud`` entry point of the
``igni.layers`` group, so the core's :class:`~igni_core.core.layer.Layers`
installs it when the image sets ``IGNI_PROFILE=cloud`` — for the backend,
``igni migrate`` and the CLI alike. What it changes:

* **logs** — JSON lines on standard output, collected by the node; no log
  file under ``~/.igni`` (:mod:`igni_cloud.logs`);
* **credentials** — the token the VM keeps from its own credential, never
  ``credentials.json`` (:mod:`igni_cloud.credentials`);
* **settings** — only the image's (command line, and the managed policy
  the node agent writes to ``/run/igni/managed-settings.yaml``) and igni
  server's (group policy); nothing under ``~/.igni`` or the workspace;
* **state** — must be in ``IGNI_DATABASE_URL``;
* **the extension listing** — the priority and group igni server sends are
  kept in the process, never written;
* **MCP approvals** — offered by group policy, approved by the person on
  igni server (:mod:`igni_cloud.mcp_approvals`);
* **the platform's calls** — ``deliver_service_events`` and
  ``vm_sign_in`` only on the gateway's control stream; igni pulls no
  events itself;
* **extension pages** — a person's desktop frames them: a connection the
  node agent marked ``X-Igni-Gateway: pages`` speaks for an extension, no
  other does, and igni's own port serves the pages' files to the gateway
  on such a stream (``igni_core.extensions.page_files``);
* **native dialogs** — none: nobody sees the VM's screen, so a call that
  would open one (``pick_dir_native``) is refused as an RPC error;
* **the group's mode** — this igni *is* the cloud a ``cloud`` group sends
  its people to, so the mode never closes its run gate;
* **the backend's boot and serving** — the guest contract with the VM's
  agents: ``ready``, late binding to its producer, ``activity.json``,
  restores, the VM's own credential (:mod:`igni_cloud.guest`).

PostgreSQL storage is not part of the layer: it is chosen by the database
URL (:mod:`igni_cloud.storage.postgres`, the ``igni.storage`` entry point
``postgresql``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import ClassVar

from igni_cloud.credentials import EnvironmentCredentials
from igni_cloud.guest.contract import CloudGuest
from igni_cloud.guest.paths import GuestPaths
from igni_cloud.logs import StdoutLog
from igni_cloud.mcp_approvals import CloudMcpApprovals
from igni_core.core.layer import BackendHooks, Layer
from igni_core.core.mcp.approval import McpApprovalPolicy


class CloudLayer(Layer):
    name: ClassVar[str] = "cloud"

    def __init__(self) -> None:
        #: The process's credentials: the guest contract tells them who keeps the token.
        self._credentials = EnvironmentCredentials()
        #: The process's standard output: the guest contract holds its writer after a restore.
        self._logs = StdoutLog()

    @property
    def logs(self) -> StdoutLog:
        return self._logs

    def log_files(self) -> bool:
        return False

    def log_handler(self) -> logging.Handler:
        # The core attaches the handler to its own loggers; the layer's
        # (the guest contract's: binding, restores, the resume timeline)
        # go to the same standard output.
        self._logs.attach("igni_cloud")
        return self._logs.handler()

    def credentials(self) -> EnvironmentCredentials:
        return self._credentials

    def reads_local_settings(self) -> bool:
        return False

    def requires_database(self) -> bool:
        return True

    def keeps_listing_in_files(self) -> bool:
        return False

    def mcp_approvals(self) -> McpApprovalPolicy | None:
        return CloudMcpApprovals()

    def managed_settings_path(self) -> Path | None:
        # The node agent writes the producer's managed policy into the run
        # directory at every start, after the token and before producer.env.
        return GuestPaths.from_env().managed_settings

    def platform_calls_need_control(self) -> bool:
        # Only the gateway's control stream: the node agent adds the header
        # to it and strips it from every client's.
        return True

    def connections_speak_for_extensions(self) -> bool:
        # Yes, but only a page host's (``extension_calls_need_page_mark``):
        # every client of a VM's igni is the producer, through the gateway,
        # and the one that frames extension pages — the person's desktop —
        # asks the gateway for a page-host stream, which the node agent
        # marks as it marks the control stream. A program that did not ask
        # has no mark, and its ``caller`` is refused.
        return True

    def extension_calls_need_page_mark(self) -> bool:
        # The node agent strips ``X-Igni-Gateway`` from every client's
        # upgrade and adds ``pages`` only on the gateway's word, given after
        # it verified a person's session: the mark cannot come from outside.
        return True

    def serves_page_files(self) -> bool:
        # The pages' own origins listen inside the VM, out of the desktop's
        # reach; the gateway fetches their files over igni's port instead.
        return True

    def fetches_own_events(self) -> bool:
        # The gateway pulls them and delivers them on its control stream.
        return False

    def opens_native_dialogs(self) -> bool:
        # A VM has no display anybody looks at: a folder picker would open
        # nowhere and its call would wait for ever. The desktop's window
        # hides the picker in cloud mode; this is the answer any other
        # client, or an old one, gets.
        return False

    def runs_on_the_persons_computer(self) -> bool:
        # The VM is where a ``cloud`` group's people run igni.
        return False

    def backend(self) -> BackendHooks:
        return CloudGuest.from_env(tokens=self._credentials, logs=self._logs)


__all__ = ["CloudLayer"]
