"""Windows: an AppContainer per run, a job object, and named pipes as the only way out.

Each run of an extension's process gets an AppContainer of its own — a
profile named for the extension and the run
(``CreateAppContainerProfile``), whose SID nothing else on the machine
has been granted — and the process gets:

* **network** — no capabilities at all: not ``internetClient``, not
  ``privateNetworkClientServer``, so no host on the internet or the
  network is reachable, and AppContainer loopback isolation (left as
  Windows sets it: igni adds no loopback exemption) blocks every port on
  this machine outside the container — igni's backend, other
  extensions' proxies and pages, any other local service. What it may
  reach — igni's egress proxy and igni's two loopback endpoints (host
  channel, token endpoint) — it reaches through named pipes whose
  access list admits this container's SID and the person, relayed by a
  runner outside and a shim inside (:mod:`.winbridge`). The shim
  listens on loopback ports of its own within the container and
  rewrites ``HTTP(S)_PROXY`` and igni's endpoint URLs to them; a
  process in an AppContainer may use loopback inside its own container.
* **files** — an AppContainer is granted nothing in the person's
  profile by default; system directories (``Windows``, ``Program
  Files``) are readable to every AppContainer already (``ALL
  APPLICATION PACKAGES``). igni adds an entry for this container's SID
  to the access list of exactly the plan's paths (:class:`~.plan.SandboxPlan`):
  read and execute on its read paths — its bundle, its Python
  environment and the interpreter that environment was made from,
  igni's own interpreter for the shim, the workspace when granted —
  and modify on its write paths, which also get a low integrity label,
  since an AppContainer runs at low integrity and cannot write up. The
  person's home and other extensions' data have no entry for it, so
  they cannot be read. A path is never granted when it is a drive root,
  the person's home or above it.
* **processes** — a job object that kills every process in it when the
  runner's handle closes, from which none may break away; the runner
  exits when the extension's process does or igni is gone, so the tree
  dies with the run.

Entries are removed and labels restored when the run is released.
Before igni changes anything it writes what it is about to change to a
ledger, one file per run under ``~/.igni/sandbox/appcontainer``, and
removes the file only once everything is undone; a run whose igni is no
longer alive is undone the next time igni starts a sandboxed process
(:meth:`AppContainer.sweep`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from igni_core.core.paths import home_config_dir
from igni_core.extensions.sandbox.plan import SandboxPlan

logger = logging.getLogger(__name__)

#: Windows 10 (build 10240) or later: AppContainers for desktop processes,
#: nested job objects and the process attribute list this uses are older,
#: but Windows 10 is what igni is tested on.
MIN_BUILD = 10240

#: A low integrity label, passed down to everything below a directory.
LOW_LABEL_DIR = "S:(ML;OICI;NW;;;LW)"
#: The same for a single file.
LOW_LABEL_FILE = "S:(ML;;NW;;;LW)"

#: How the launcher names a run directory (``igni-ext-<id>-<random>``).
RUN_DIR_PREFIX = "igni-ext-"

#: AppContainer names: at most 64 characters.
MAX_PROFILE_NAME = 64

#: Environment variables naming system directories every AppContainer
#: may already read; a read grant inside one is not needed.
SYSTEM_DIRS_ENV = ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")


class WindowsCalls(Protocol):
    """The Win32 calls :class:`AppContainer` makes (:mod:`.winapi`); tests pass a fake."""

    def create_profile(self, name: str, display: str, description: str) -> str: ...
    def delete_profile(self, name: str) -> None: ...
    def current_user_sid(self) -> str: ...
    def grant(self, path: Path, sid: str, *, write: bool) -> None: ...
    def revoke(self, path: Path, sid: str) -> None: ...
    def label(self, path: Path) -> str: ...
    def set_label(self, path: Path, sddl: str) -> None: ...
    def process_alive(self, pid: int) -> bool: ...


class _Win32:
    """:class:`WindowsCalls` on this machine."""

    def __getattr__(self, name: str) -> object:
        from igni_core.extensions.sandbox import winapi

        return getattr(winapi, name)


@dataclass(slots=True)
class Grant:
    """One path a container's SID is given, and what it was before."""

    path: Path
    write: bool
    #: For a write grant: the path's integrity label before igni lowered
    #: it, as SDDL (``""`` when it had none), so it can be put back.
    label: str | None = None


@dataclass(slots=True)
class ContainerRun:
    """One run's container: what was created and granted, to undo."""

    profile: str
    sid: str
    #: igni's process id: while it is alive, the run is not a leftover.
    owner: int
    run_dir: str = ""
    grants: list[Grant] = field(default_factory=list)

    def to_json(self) -> str:
        data = asdict(self)
        data["grants"] = [{**asdict(g), "path": str(g.path)} for g in self.grants]
        return json.dumps(data, indent=2)

    @classmethod
    def from_json(cls, text: str) -> ContainerRun:
        data = json.loads(text)
        grants = [
            Grant(path=Path(g["path"]), write=bool(g["write"]), label=g.get("label"))
            for g in data.get("grants", [])
        ]
        return cls(
            profile=str(data["profile"]),
            sid=str(data["sid"]),
            owner=int(data["owner"]),
            run_dir=str(data.get("run_dir", "")),
            grants=grants,
        )


class GrantLedger:
    """What igni has granted AppContainers, one file per run, written before it is done."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _file(self, profile: str) -> Path:
        return self.directory / f"{profile}.json"

    def record(self, run: ContainerRun) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self._file(run.profile)
        partial = target.with_suffix(".json.partial")
        partial.write_text(run.to_json())
        os.replace(partial, target)

    def remove(self, profile: str) -> None:
        self._file(profile).unlink(missing_ok=True)

    def entries(self) -> list[ContainerRun]:
        if not self.directory.is_dir():
            return []
        out: list[ContainerRun] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                out.append(ContainerRun.from_json(path.read_text()))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                logger.warning("unreadable sandbox ledger entry %s: %s", path, exc)
        return out


def explicit_label(sddl: str) -> str:
    """The part of a label igni must put back: its explicit entries, not inherited ones.

    ``S:AI(ML;ID;NW;;;LW)`` — inherited from a parent — becomes ``S:``,
    which removes the label igni set and lets the parent's flow back.
    """
    kept: list[str] = []
    for ace in re.findall(r"\(([^)]*)\)", sddl):
        fields = ace.split(";")
        flags = fields[1] if len(fields) > 1 else ""
        if "ID" not in [flags[i : i + 2] for i in range(0, len(flags), 2)]:
            kept.append(ace)
    return "S:" + "".join(f"({ace})" for ace in kept)


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _too_broad(path: Path, home: Path | None) -> bool:
    """A drive root, the person's home, or a directory above it."""
    if len(path.parts) <= 1:
        return True
    return home is not None and _within(home, path)


class AppContainer:
    """Creates a run's AppContainer, grants it its paths, and writes the runner's argv."""

    name = "Windows AppContainer"

    def __init__(
        self, *, calls: WindowsCalls | None = None, ledger_dir: Path | None = None
    ) -> None:
        self._calls: WindowsCalls = calls if calls is not None else _Win32()  # type: ignore[assignment]
        self._ledger_dir = ledger_dir
        self._swept = False

    @property
    def ledger(self) -> GrantLedger:
        return GrantLedger(self._ledger_dir or home_config_dir() / "sandbox" / "appcontainer")

    @staticmethod
    async def probe() -> str | None:
        """``None`` when an AppContainer can confine a process here, else why not."""
        if sys.platform != "win32":
            return "Windows AppContainers exist only on Windows"
        build = sys.getwindowsversion().build
        if build < MIN_BUILD:
            return f"Windows build {build} is older than Windows 10, which the sandbox needs"
        from igni_core.extensions.sandbox import winapi

        def check() -> str | None:
            try:
                winapi.appcontainer_api_present()
                if winapi.in_appcontainer():
                    return "igni itself runs in an AppContainer, which cannot start another"
                winapi.current_user_sid()
            except OSError as exc:
                return f"AppContainers are not available here: {exc}"
            return None

        return await asyncio.to_thread(check)

    # ── names ───────────────────────────────────────────────────────

    @staticmethod
    def profile_name(extension_id: str, token: str) -> str:
        """``igni.<extension>.<token>``: readable in a profile list, unique per run."""
        safe = re.sub(r"[^A-Za-z0-9.-]", "-", extension_id).strip(".-") or "ext"
        room = MAX_PROFILE_NAME - len("igni.") - len(token) - 1
        return f"igni.{safe[:room].rstrip('.-')}.{token}"

    @staticmethod
    def pipe_name(profile: str, port: int) -> str:
        """The pipe for one of igni's ports, for one run."""
        return f"\\\\.\\pipe\\{profile}-{port}"

    @staticmethod
    def bridge_dir(plan: SandboxPlan) -> Path:
        """Where the runner and shim are copied; the container may read it."""
        assert plan.run_dir is not None
        return plan.run_dir / "bridge"

    # ── what it is granted ──────────────────────────────────────────

    @staticmethod
    def grants(
        plan: SandboxPlan,
        *,
        extra_read: list[Path],
        home: Path | None,
        environ: Mapping[str, str],
    ) -> list[Grant]:
        """The access-list entries the plan needs, fewest that cover it.

        A path below one already granted at least as much is left out
        (an entry on a directory is inherited by everything in it); a
        read path inside a system directory every AppContainer reads is
        left out; a path that does not exist cannot carry an entry. A
        drive root, the person's home and anything above it are never
        granted, whatever the plan says.
        """
        system = [Path(environ[name]) for name in SYSTEM_DIRS_ENV if environ.get(name)]
        wanted: dict[Path, bool] = {}
        for path in [*plan.read, *extra_read]:
            wanted.setdefault(path, False)
        for path in plan.write:
            wanted[path] = True
        out: list[Grant] = []
        for path, write in sorted(wanted.items(), key=lambda kv: (len(kv[0].parts), str(kv[0]))):
            if _too_broad(path, home):
                logger.warning(
                    "extension %s: %s is not granted to its sandbox: a drive root or the "
                    "person's home is too broad",
                    plan.extension_id,
                    path,
                )
                continue
            if not path.exists():
                continue
            if not write and any(_within(path, root) for root in system):
                continue
            if any(_within(path, g.path) and (g.write or not write) for g in out):
                continue
            out.append(Grant(path=path, write=write))
        return out

    @staticmethod
    def environment(plan: SandboxPlan) -> dict[str, str]:
        """Windows' own names for home and app data, inside the run's home."""
        home = plan.home_dir
        roaming = home / "AppData" / "Roaming"
        local = home / "AppData" / "Local"
        for path in (roaming, local):
            path.mkdir(parents=True, exist_ok=True)
        return {"USERPROFILE": str(home), "APPDATA": str(roaming), "LOCALAPPDATA": str(local)}

    # ── a run ───────────────────────────────────────────────────────

    def start(
        self,
        plan: SandboxPlan,
        command: str,
        args: list[str],
        *,
        cwd: Path | None,
        forwards: list[int],
        python: str | None = None,
        home: Path | None = None,
    ) -> tuple[ContainerRun, str, list[str]]:
        """Create the container, grant it the plan, and return the runner's argv.

        Blocking (access lists on a large tree take a while); the
        launcher calls it in a thread. On failure everything done so far
        is undone before the error is raised.
        """
        assert plan.run_dir is not None
        if not self._swept:
            self.sweep()
        python = python or sys.executable
        bridge = self.bridge_dir(plan)
        bridge.mkdir(exist_ok=True)
        script = bridge / "winbridge.py"
        shutil.copyfile(Path(__file__).with_name("winbridge.py"), script)

        profile = self.profile_name(plan.extension_id, secrets.token_hex(6))
        person = home if home is not None else Path(os.path.expanduser("~"))
        grants = self.grants(
            plan,
            extra_read=[bridge, *SandboxPlan.runtime_paths(python, dict(os.environ))],
            home=person,
            environ=os.environ,
        )
        sid = self._calls.create_profile(
            profile, f"igni: {plan.extension_id}", f"igni sandbox for {plan.extension_id}"
        )
        run = ContainerRun(profile=profile, sid=sid, owner=os.getpid(), run_dir=str(plan.run_dir))
        try:
            for grant in grants:
                if grant.write:
                    grant.label = self._read_label(grant.path)
            run.grants = grants
            # Written before anything changes, so a crash leaves a record.
            self.ledger.record(run)
            run.grants = [g for g in grants if self._apply(plan.extension_id, run.sid, g)]
            self.ledger.record(run)
            user = self._calls.current_user_sid()
        except BaseException:
            self.stop(run)
            raise

        argv = [
            "-I",
            str(script),
            "run",
            "--sid",
            sid,
            "--user",
            user,
            "--parent",
            str(os.getpid()),
            "--python",
            python,
            "--cwd",
            str(cwd or plan.temp_dir),
        ]
        for port in forwards:
            argv += ["--forward", f"{port}={self.pipe_name(profile, port)}"]
        return run, python, [*argv, "--", command, *args]

    def _read_label(self, path: Path) -> str | None:
        try:
            return self._calls.label(path)
        except OSError as exc:
            logger.warning(
                "could not read the integrity label of %s, so not lowering it: %s", path, exc
            )
            return None

    def _apply(self, extension_id: str, sid: str, grant: Grant) -> bool:
        """Grant one path; ``False`` for a read grant that could not be made.

        A label that cannot be lowered is a warning, not a refusal: the
        process then fails to write there, visibly, and igni has nothing
        to put back.
        """
        try:
            self._calls.grant(grant.path, sid, write=grant.write)
        except OSError as exc:
            if grant.write:
                raise
            # Not the person's to change (a shared interpreter, say): the
            # process reads it if every AppContainer may, and fails
            # visibly if not.
            logger.warning(
                "extension %s: could not grant its sandbox read access to %s: %s",
                extension_id,
                grant.path,
                exc,
            )
            return False
        if grant.write and grant.label is not None:
            low = LOW_LABEL_DIR if grant.path.is_dir() else LOW_LABEL_FILE
            try:
                self._calls.set_label(grant.path, low)
            except OSError as exc:
                logger.warning(
                    "extension %s: could not lower the integrity label of %s, which its "
                    "sandbox may then not write: %s",
                    extension_id,
                    grant.path,
                    exc,
                )
                grant.label = None
        return True

    def stop(self, run: ContainerRun) -> None:
        """Remove the run's entries, put labels back, delete its profile and record."""
        failed = False
        for grant in reversed(run.grants):
            try:
                self._calls.revoke(grant.path, run.sid)
                if grant.write and grant.label is not None:
                    self._calls.set_label(grant.path, explicit_label(grant.label))
            except FileNotFoundError:
                continue
            except OSError as exc:
                failed = True
                logger.warning(
                    "sandbox %s: could not remove its access to %s: %s",
                    run.profile,
                    grant.path,
                    exc,
                )
        try:
            self._calls.delete_profile(run.profile)
        except OSError as exc:
            failed = True
            logger.warning("sandbox %s: could not delete its profile: %s", run.profile, exc)
        if not failed:
            self.ledger.remove(run.profile)

    def sweep(self) -> int:
        """Undo the runs an igni that is no longer running left behind; how many."""
        self._swept = True
        undone = 0
        for run in self.ledger.entries():
            if run.owner == os.getpid() or self._calls.process_alive(run.owner):
                continue
            logger.warning(
                "undoing sandbox %s left behind by igni process %d", run.profile, run.owner
            )
            self.stop(run)
            # Only a directory the launcher made: the ledger names it,
            # and a damaged ledger must not name something else to delete.
            if run.run_dir and Path(run.run_dir).name.startswith(RUN_DIR_PREFIX):
                shutil.rmtree(run.run_dir, ignore_errors=True)
            undone += 1
        return undone


__all__ = [
    "AppContainer",
    "ContainerRun",
    "Grant",
    "GrantLedger",
    "WindowsCalls",
    "explicit_label",
]
