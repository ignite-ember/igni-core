"""``igni ext`` — build, check and install extensions while developing them.

Extensions reach people's machines from their services, through igni
server: a service uploads its signed bundle, a super admin approves the
service, an organisation admin switches the extension on for a group,
and the igni of everyone in that group installs it at its next sync
(igni server's ``docs/service-identity.md`` §8). There is no extension
marketplace and no policy that pushes one.

What is left here is the author's side. ``init`` scaffolds one,
``doctor`` checks one, ``sync`` fetches what igni server lists now,
and ``install`` runs one from a directory or a
repository — a **development install**, which needs
``IGNI_DEV_EXTENSIONS=1`` because it is the one way onto a machine that
does not go through igni.

Everything here works against the **store** rather than a running
backend. An install is a decision that outlives a process: the bundle
is copied under ``<data>/extensions/<id>/`` and the backend reads
that directory at boot. The alternative — a CLI that talks to a live
backend over RPC — would mean nothing could be installed before one
was running, and that an install vanished when it stopped.

``doctor`` reads and never writes, which is what makes it safe to
tell somebody to run first.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import click
from pydantic import ValidationError

from igni_core.cli.ext_doctor import Findings, NameCheck, ProcessProbe
from igni_core.cli.ext_manifest_check import ManifestCheck
from igni_core.cli.ext_scaffold import Scaffold
from igni_core.core.config.group_policy.policy_settings import PolicySettingsLoader
from igni_core.core.config.group_policy.settings_fetcher import GroupPolicyUnavailable
from igni_core.core.config.settings import Settings
from igni_core.core.network_policy import NetworkPolicy
from igni_core.core.plugins.git import GitClient
from igni_core.extensions.bundle_source import BundleSource, BundleSourceError
from igni_core.extensions.distribution import OneShotSync
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.profile import DEV_FLAG, DevelopmentFlag
from igni_core.extensions.store import ExtensionStore, StoreError

#: What this build of igni implements. An extension declares a range
#: and this is what it is checked against — the only compatibility
#: axis, and never an igni version.
CONTRACT_VERSION = "1.0"


@click.group("ext")
def ext() -> None:
    """Build, check and install extensions."""


def _store(root: str | None) -> ExtensionStore:
    return ExtensionStore(Path(root).expanduser() if root else None)


@ext.command("doctor")
@click.argument(
    "bundle",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=".",
)
@click.option(
    "--no-start",
    is_flag=True,
    help="Check the manifest only; do not start the process.",
)
@click.option(
    "--timeout",
    type=float,
    default=60.0,
    show_default=True,
    help="Seconds the process has to answer MCP's initialize and tools/list.",
)
def doctor(bundle: Path, no_start: bool, timeout: float) -> None:
    """Check a bundle and say what is wrong with it.

    Checks the manifest, then starts the process the way igni would —
    a 'dev' command from the bundle, a 'python' package installed into
    a throwaway environment — and asks it, over MCP, for its tools.
    Writes nothing outside a temporary directory, so it is safe to run
    first and safe to run on somebody else's directory. A 'python'
    source checkout with no wheelhouse is built, which needs its
    dependencies on an index or under PIP_FIND_LINKS.
    """
    click.echo(f"contract implemented by this igni: {CONTRACT_VERSION}")

    try:
        manifest = ExtensionStore().read(bundle)
    except StoreError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"{manifest.display_name} ({manifest.id} {manifest.version})")
    click.echo(f"  contract   {manifest.contract}")
    click.echo(f"  trust      {manifest.trust.value}")
    click.echo(f"  packaging  {manifest.packaging}")

    findings = Findings()
    ManifestCheck(manifest, bundle).check(findings)
    names = NameCheck(manifest)
    names.check(findings)

    if findings.notes:
        click.echo()
        for note in findings.notes:
            click.echo(f"  - {note}")
    shown = len(findings.notes)

    click.echo()
    if no_start:
        click.echo("  process    not started (--no-start)")
    elif findings.problems:
        click.echo("  process    not started: fix the problems below first")
    elif manifest.endpoint is not None:
        click.echo("  process    not started: an endpoint is somebody else's process")
    else:
        probe = asyncio.run(ProcessProbe(manifest, bundle, timeout=timeout).run())
        if probe.error is not None:
            findings.problems.append(f"starting it: {probe.error}")
        else:
            click.echo(f"  process    started it and asked for its tools: {len(probe.tools)}")
            if probe.tools:
                click.echo()
                click.echo("  each tool's names (the server's own, then where each is used):")
                for row in names.table(probe.tools):
                    click.echo(row)
            names.compare(probe.tools, findings)

    for note in findings.notes[shown:]:
        click.echo(f"  - {note}")
    if findings.warnings:
        click.echo()
        for warning in findings.warnings:
            click.echo(f"  ~ warning: {warning}")

    if findings.problems:
        click.echo()
        for problem in findings.problems:
            click.echo(f"  ! {problem}")
        raise click.ClickException(
            f"{len(findings.problems)} problem(s) found — it will not install as it is"
        )

    click.echo()
    click.echo("  no problems found")


def _settings() -> Settings:
    """The deployment's settings, group policy included, as ``igni -m`` loads them.

    Neither pass touches the network, so an organisation that turns
    ``air_gapped`` on through its group policy has it apply here too.
    """
    try:
        return PolicySettingsLoader().load()
    except GroupPolicyUnavailable as exc:
        raise click.ClickException(str(exc)) from exc


@contextmanager
def _bundle(source: BundleSource, network: NetworkPolicy) -> Iterator[Path]:
    """The bundle ``source`` names, saying what was cloned when it is a repository."""
    if not source.is_repository:
        yield source.directory()
        return
    at_ref = f" at {source.ref}" if source.ref else ""
    with source.clone(
        network,
        GitClient(),
        on_start=lambda: click.echo(f"cloning {source.source}{at_ref} …"),
    ) as checkout:
        # What was taken, as a fact rather than as advice. Whether to pin is
        # the installer's call: "the latest" is a legitimate answer, and this
        # is the commit it meant today.
        click.echo(f"  at {checkout.commit}")
        yield checkout.bundle


@ext.command("install")
@click.argument("source")
@click.option(
    "--replace",
    is_flag=True,
    help="Upgrade an already-installed extension. Re-asks for its permissions.",
)
@click.option(
    "--ref",
    default=None,
    help=(
        "Branch, tag or commit to install from, when SOURCE is a repository. "
        "Without it you get the default branch as it stands now, which is a "
        "choice rather than an oversight."
    ),
)
@click.option(
    "--subdir",
    default=None,
    help="The bundle's directory inside the repository, when it is not the root.",
)
@click.option("--root", default=None, help="Install somewhere other than the default.")
def install(
    source: str, replace: bool, ref: str | None, subdir: str | None, root: str | None
) -> None:
    """Install a bundle you are developing.

    SOURCE is a directory or a repository URL. This is a development
    install, and it needs ``IGNI_DEV_EXTENSIONS=1``: extensions reach
    machines from their services through igni server, and this is the
    one way onto a machine that does not. The backend refuses to start
    a development install without the flag too, so setting it here
    and not there gets you a stored bundle that does not run.

    A ``python``-packaged source checkout with no wheelhouse is built
    from the directory when the backend starts it, network allowed.

    A development install of an id igni also distributes wins on this
    machine: the sync leaves it alone until you remove it.

    Copies it into the store. The backend starts it at its next boot
    — this command does not reach into a running one, so a backend
    that is up will not see it until it restarts.
    """
    if not DevelopmentFlag.enabled():
        raise click.ClickException(
            "Installing from a directory or a repository is for developing an "
            f"extension, and needs {DEV_FLAG}=1. Extensions reach this machine "
            "from their services through igni server when you are signed in."
        )
    store = _store(root)
    network = NetworkPolicy.from_settings(_settings())
    try:
        with _bundle(BundleSource(source, ref=ref, subdir=subdir), network) as bundle:
            manifest = store.read(bundle)
            target = store.add(bundle, replace=replace)
    except (BundleSourceError, StoreError) as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"installed {manifest.display_name} ({manifest.id}) to {target} for development")
    _echo_grants(manifest)
    click.echo()
    click.echo(_STARTS)


#: What to restart, said exactly: "restart igni" read as "run igni
#: again", and the app's backend kept running with what it had.
_STARTS = (
    f"It starts the next time igni's backend starts: quit the igni app (and any "
    f"'igni' backend process it left running) and open it again with {DEV_FLAG}=1 "
    f"in its environment. An 'igni -m ...' run with {DEV_FLAG}=1 starts it too."
)
_STOPS = (
    "It stops the next time igni's backend starts: quit the igni app (and any "
    "'igni' backend process it left running) and open it again."
)


def _echo_grants(manifest: ExtensionManifest) -> None:
    """Print what this extension was granted.

    Shown on install because that is the moment an administrator is
    deciding, and because a permission nobody reads is the same as a
    permission nobody enforces.
    """
    permissions = manifest.permissions
    granted = [
        ("workspace", permissions.workspace.value),
        ("workspace paths", ", ".join(permissions.workspacePaths) or "all"),
        ("egress", ", ".join(permissions.egress) or "none"),
        ("secrets", ", ".join(permissions.secrets) or "none"),
        ("agent tools", ", ".join(tool.name for tool in permissions.tools) or "none"),
        ("backend methods", ", ".join(permissions.rpc) or "none"),
        ("host objects", ", ".join(permissions.host) or "none"),
    ]
    click.echo()
    click.echo("  it may use:")
    for label, value in granted:
        click.echo(f"    {label:<16} {value}")


@ext.command("sync")
@click.option("--root", default=None, help="Sync somewhere other than the default.")
@click.option(
    "--timeout",
    type=float,
    default=120.0,
    show_default=True,
    help="Seconds igni server has to list and send everything.",
)
def sync(root: str | None, timeout: float) -> None:
    """Install, update and remove extensions to match igni server, now.

    What the igni app does at start-up and at sign-in, for a terminal:
    igni server lists the extensions switched on for the signed-in
    person's group, and this brings the store in step — downloading,
    verifying and storing what is new or changed, removing what is no
    longer listed, and leaving a development install of the same id
    alone. It starts and stops nothing: igni's backend starts what is
    stored when it boots, and an ``igni -m`` run syncs by itself first.
    """
    store = _store(root)
    settings = _settings()
    syncer = OneShotSync(store, settings)
    report = asyncio.run(syncer.run(timeout=timeout))
    if report.skipped is not None:
        raise click.ClickException(f"nothing was synced: {report.skipped}")

    group = store.group()
    who = f" (group {group.label})" if group is not None and group.label else ""
    click.echo(f"synced with {syncer.api_url}{who}")
    rows: list[tuple[str, str]] = []
    for extension_id in report.installed:
        record = store.distribution_of(extension_id)
        source = f" {record.version} from {record.service_url}" if record else ""
        rows.append(("installed", f"{extension_id}{source}"))
    rows += [("unchanged", extension_id) for extension_id in report.unchanged]
    rows += [("removed", f"{extension_id} (no longer listed)") for extension_id in report.removed]
    rows += [
        (
            "kept",
            f"{extension_id}: your development install stays, and the version igni "
            f"distributes is not installed while it is there "
            f"('igni ext remove {extension_id}' to take it)",
        )
        for extension_id in report.development
    ]
    rows += [("failed", f"{extension_id}: {why}") for extension_id, why in report.failed.items()]
    for label, text in rows:
        click.echo(f"  {label:<10} {text}")
    if not rows:
        click.echo(
            "  nothing is switched on for you. An extension arrives once a super admin "
            "has approved its service (portal, Services) and an organisation admin has "
            "switched it on for your group (portal, Extensions)."
        )
    if report.installed or report.removed:
        click.echo()
        click.echo(
            "What changed applies the next time igni's backend starts: quit the igni app "
            "(and any 'igni' backend process it left running) and open it again. An "
            "'igni -m ...' run starts what is installed by itself."
        )
    if report.failed:
        raise click.ClickException(f"{len(report.failed)} extension(s) could not be installed")


@ext.command("list")
@click.option("--root", default=None, help="Read somewhere other than the default.")
def list_installed(root: str | None) -> None:
    """What is installed in this deployment."""
    store = _store(root)
    bundles = store.installed()
    if not bundles:
        click.echo(f"nothing installed in {store.root}")
        return
    for bundle in bundles:
        try:
            manifest = store.read(bundle)
        except StoreError as exc:
            click.echo(f"{bundle.name:<28} unreadable — {exc}")
            continue
        record = store.distribution_of(manifest.id)
        source = f"from {record.service_url}" if record is not None else "development install"
        click.echo(f"{manifest.id:<28} {manifest.version:<10} {source}")


@ext.command("remove")
@click.argument("extension_id")
@click.option("--root", default=None, help="Read somewhere other than the default.")
def remove(extension_id: str, root: str | None) -> None:
    """Uninstall an extension from this deployment.

    One igni distributed comes back at the next sync for as long as
    its service is approved, and this says so rather than letting the
    removal look permanent.
    """
    store = _store(root)
    try:
        distributed = store.distribution_of(extension_id)
        removed = store.remove(extension_id)
    except StoreError as exc:
        raise click.ClickException(str(exc)) from exc
    if not removed:
        raise click.ClickException(f"{extension_id} is not installed")
    click.echo(f"removed {extension_id}")
    if distributed is not None:
        click.echo(
            f"It came from {distributed.service_url} through igni, and is installed "
            "again at the next sync while that service is approved."
        )
    click.echo(_STOPS)


@ext.command("init")
@click.argument("directory", type=click.Path(path_type=Path))
@click.option("--id", "extension_id", default=None, help="Reverse-DNS id, e.g. acme.tickets.")
@click.option(
    "--dev",
    "shape",
    flag_value="dev",
    default=True,
    help="(default) 'dev' packaging: a command igni runs on this machine. Never leaves it.",
)
@click.option(
    "--service",
    "shape",
    flag_value="service",
    help=(
        "'python' packaging and the service that ships it: two tools, a hook, a page, "
        "a VM request, export and erasure, and tests on the SDKs' fakes."
    ),
)
def init(directory: Path, extension_id: str | None, shape: str) -> None:
    """Scaffold an extension that runs.

    There are four packagings. ``--dev`` (the default) is a command igni
    runs on this machine — the quickest to start, and it reaches nobody
    else. ``--service`` is ``python`` packaging with the service that
    ships it through igni server, which is how an extension reaches
    other people. ``endpoint`` and ``sidecar`` need a deployed service
    or a container registry, so they are not scaffolded.

    What this writes installs and starts as-is — a scaffold that needs
    edits before it works is a scaffold that teaches nothing about
    whether the install path is sound.
    """
    if directory.exists() and any(directory.iterdir()):
        raise click.ClickException(f"{directory} is not empty")

    scaffold = Scaffold(directory, extension_id)
    try:
        written = scaffold.write_service() if shape == "service" else scaffold.write_dev()
    except ValidationError as error:
        # The manifest is built before anything is written, and the id
        # is the only thing in it the author chose.
        raise click.ClickException(
            f"{scaffold.id!r} is not a usable extension id: give one with --id, "
            "reverse-DNS, lower case and without hyphens, e.g. --id acme.tickets\n"
            f"{error}"
        ) from error

    click.echo(f"created {directory} ({scaffold.id})")
    for name in written:
        click.echo(f"  {name}")
    click.echo()
    click.echo("next:")
    if shape == "service":
        click.echo(f"  cd {directory} && uv sync && uv run pytest")
        click.echo(f"  igni ext doctor {directory / 'extension'}")
        click.echo(f"  then {directory / 'README.md'} — shipping it through igni server")
    else:
        click.echo(f"  igni ext doctor {directory}")
        click.echo(f"  {DEV_FLAG}=1 igni ext install {directory}")
