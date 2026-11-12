"""What ``igni ext init`` writes.

Four packagings exist — ``python``, ``dev``, ``endpoint`` and
``sidecar`` — and two of them are scaffolded:

* **dev** (``--dev``, the default): a command igni runs on this machine.
  The quickest thing that installs and starts, and the one way to run
  an extension with nothing else deployed. It never leaves the laptop.
* **service** (``--service``): what reaches other people — a
  ``python``-packaged extension (two tools, a hook, a page, a VM
  request), the service that ships it (``igni-service``, with export
  and erasure), and tests for both on the SDKs' fakes.

``endpoint`` (a service someone else runs) and ``sidecar`` (a container
image, which igni cannot start yet) need something an author does not
have on a laptop, so they are not scaffolded.

The page is a file and its script another: an extension's origin serves
it under ``script-src 'self'`` (:mod:`igni_core.extensions.origin`),
which refuses an inline ``<script>`` — the first scaffold had one, so
the page it wrote never ran.
"""

from __future__ import annotations

import html
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path

from igni_core.cli import ext_scaffold_templates as templates
from igni_core.core.hooks.events import HookEvent
from igni_core.core.mcp.hints import ToolHints
from igni_core.extensions.manifest import (
    Contributes,
    DevCommand,
    ExtensionManifest,
    McpBinding,
    PageContribution,
    Permissions,
    PythonPackage,
    ToolGrant,
    Trust,
    VmPackages,
    VmRequest,
    Workspace,
)

#: Where the manifest's JSON Schema is published. The same file is
#: ``docs/extension-manifest.schema.json`` in igni's repository.
SCHEMA_URL = "https://ignite-ember.sh/schema/extension-manifest-1.json"

#: A ``{{TOKEN}}`` in a template.
_TOKEN = re.compile(r"\{\{([A-Z_]+)\}\}")


class Scaffold:
    """Writes one scaffold into an empty directory.

    The manifest is built as an :class:`ExtensionManifest` before any
    file is written, so an id the contract refuses fails with
    :class:`pydantic.ValidationError` and leaves the directory as it was.
    """

    def __init__(self, directory: Path, extension_id: str | None) -> None:
        self.directory = directory
        self.title = directory.resolve().name
        # No hyphen: an id is part of every RPC method it grants itself,
        # and an RPC grant takes none.
        self.id = extension_id or "local." + re.sub(r"[^a-z0-9]+", "", self.title.lower())

    # ── names ─────────────────────────────────────────────────────

    @property
    def module(self) -> str:
        """The extension's Python package: ``acme.tickets`` → ``acme_tickets_extension``."""
        return re.sub(r"[^a-z0-9]+", "_", self.id.lower()).strip("_") + "_extension"

    @property
    def distribution(self) -> str:
        return self.module.replace("_", "-")

    @property
    def label(self) -> str:
        """The sidebar label: the title, as much of it as fits."""
        return self.title[:24]

    # ── manifests ─────────────────────────────────────────────────

    def dev_manifest(self) -> ExtensionManifest:
        return ExtensionManifest(
            schema_=SCHEMA_URL,
            id=self.id,
            version="0.1.0",
            contract=">=1.0 <2",
            name=self.title,
            trust=Trust.UNSIGNED,
            # The interpreter igni runs on: it has the mcp package
            # server.py imports, and a bare "python" is on no PATH on a
            # stock Mac. Point it at your own environment's once
            # server.py needs more than mcp.
            dev=DevCommand(command=[sys.executable, "server.py"]),
            permissions=Permissions(rpc=[f"ext.{self.id}.*"]),
            contributes=Contributes(
                pages=[PageContribution(kind=f"{self.id}.main", label=self.label, sidebar=True)]
            ),
        )

    def service_manifest(self) -> ExtensionManifest:
        return ExtensionManifest(
            schema_=SCHEMA_URL,
            id=self.id,
            version="0.1.0",
            contract=">=1.0 <2",
            name=self.title,
            description="Look up a support ticket by its id.",
            vendor="You",
            trust=Trust.PARTNER,
            python=PythonPackage(
                package=self.distribution,
                entry=self.distribution,
                wheels="wheels",
                mcp=McpBinding(transport="stdio"),
            ),
            permissions=Permissions(
                workspace=Workspace.NONE,
                tools=[
                    ToolGrant(name=f"{self.id}.lookup_ticket", annotations=self._read(False)),
                    ToolGrant(name=f"{self.id}.ticket_from_service", annotations=self._read(True)),
                ],
                rpc=[f"ext.{self.id}.lookup_ticket"],
                hooks=[HookEvent.POST_TOOL_USE],
            ),
            vm=VmRequest(memoryMib=2048, packages=VmPackages(apt=["jq"])),
            contributes=Contributes(
                pages=[PageContribution(kind=self.id, label=self.label, sidebar=True)]
            ),
        )

    @staticmethod
    def _read(open_world: bool) -> ToolHints:
        """A tool that reads and changes nothing, inside its own system or beyond it."""
        return ToolHints(read_only=True, destructive=False, idempotent=True, open_world=open_world)

    # ── the two scaffolds ─────────────────────────────────────────

    def write_dev(self) -> list[str]:
        self._write_manifest("manifest.json", self.dev_manifest())
        self._render("server.py", templates.DEV_SERVER)
        self._page("ui", templates.DEV_BODY, templates.DEV_ON_HELLO)
        self._render("README.md", templates.DEV_README)
        return ["manifest.json", "server.py", "ui/index.html", "ui/main.js", "README.md"]

    def write_service(self) -> list[str]:
        self._write_manifest("extension/manifest.json", self.service_manifest())
        self._render("extension/pyproject.toml", templates.SERVICE_EXTENSION_PYPROJECT)
        self._render("extension/README.md", templates.SERVICE_EXTENSION_README)
        self._render(f"extension/src/{self.module}/__init__.py", templates.SERVICE_EXTENSION)
        self._page("extension/ui", templates.SERVICE_BODY, templates.SERVICE_ON_HELLO)
        self._render("service/service.py", templates.SERVICE_APP)
        self._render("tests/test_extension_and_service.py", templates.SERVICE_TESTS)
        self._render("pyproject.toml", templates.SERVICE_WORKSPACE)
        self._render("README.md", templates.SERVICE_README)
        return [
            "README.md",
            "pyproject.toml",
            "extension/manifest.json",
            "extension/pyproject.toml",
            "extension/README.md",
            f"extension/src/{self.module}/__init__.py",
            "extension/ui/index.html",
            "extension/ui/main.js",
            "service/service.py",
            "tests/test_extension_and_service.py",
        ]

    # ── writing ───────────────────────────────────────────────────

    def _fill(self, template: str, parts: Mapping[str, str] | None = None) -> str:
        """Every ``{{TOKEN}}`` in one pass; a token nobody fills is a ``KeyError``."""
        tokens = {
            "ID": self.id,
            "TITLE": self.title,
            # The title is a directory name, so anything: where it lands in
            # markup or in a Python docstring it is escaped for that.
            "TITLE_HTML": html.escape(self.title, quote=False),
            "TITLE_PY": self.title.replace("\\", "\\\\").replace('"', '\\"'),
            "MODULE": self.module,
            "DIST": self.distribution,
            **(parts or {}),
        }
        return _TOKEN.sub(lambda match: tokens[match[1]], template)

    def _write(self, relative: str, text: str) -> None:
        target = self.directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def _render(self, relative: str, template: str, parts: Mapping[str, str] | None = None) -> None:
        self._write(relative, self._fill(template, parts))

    def _write_manifest(self, relative: str, manifest: ExtensionManifest) -> None:
        """Only what the scaffold set, with ``$schema`` first where an editor looks."""
        fields = manifest.model_dump(mode="json", by_alias=True, exclude_unset=True)
        ordered = {"$schema": fields.pop("$schema"), **fields}
        self._write(relative, json.dumps(ordered, indent=2) + "\n")

    def _page(self, ui: str, body: str, on_hello: str) -> None:
        # A part is filled before it goes in: the one pass does not
        # look again at what it inserted.
        self._render(f"{ui}/index.html", templates.PAGE, {"BODY": self._fill(body)})
        self._render(f"{ui}/main.js", templates.BRIDGE, {"ON_HELLO": self._fill(on_hello)})
