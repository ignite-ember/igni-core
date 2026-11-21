"""The account-only backend starts no heavier than the full one, and says how heavy.

``docs/design/local-cloud-switch.md`` §12.1: "Startup memory is measured
and recorded." A desktop in ``cloud`` mode keeps
``python -m igni_core.backend --account-only`` running beside the
person's cloud igni, for signing in and the group pack (§6.4). It starts
no agent, tools, MCP server, extension, watcher or event pull, so it must
not cost this computer more than the full backend it stands in for.

Both are started as the shell starts them, each in a home of its own with
the same configuration, and each one's resident memory is sampled for a
short while after its ready line. The peaks are compared, and written to
the test's output (and to the JUnit report as properties) on every run, so
the number is on record wherever the suite runs.

Two checks, both generous on purpose, so that CI's machines and coverage
tracing do not make them flaky:

- the account-only peak is at most the full backend's peak plus
  :data:`NOISE` (sampling and allocator noise between two processes);
- and it is below :data:`ACCOUNT_ONLY_BOUND_MIB`.

Measured on 2026-11-21 (macOS, arm64, Python 3.12): account-only 177 MiB,
full 196 MiB; an earlier run measured 184 MiB against 194 MiB. The bound
is well above that, and is there to catch the account-only backend
growing into a second full one (an import of the agent stack, a session
built at start), not to track a few MiB.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

#: The account-only backend's startup RSS may not exceed this (MiB).
ACCOUNT_ONLY_BOUND_MIB = 320.0
#: How much heavier than the full backend's peak a sample may read, as a fraction.
NOISE = 0.10
#: How long after its ready line each backend is sampled, and how often (seconds).
SAMPLE_FOR = 2.0
SAMPLE_EVERY = 0.25
#: The longest a backend may take to print its ready line (seconds).
READY_WITHIN = 120.0

#: The same for both: a model that is never dialled (the full backend
#: needs one to resolve), no update check, no marketplace.
CONFIG = (
    "update_check_ttl: 0\n"
    "plugins:\n"
    "  default_marketplace: false\n"
    "models:\n"
    "  default: stub\n"
    "  registry:\n"
    "    stub:\n"
    "      provider: openai_like\n"
    "      model_id: stub\n"
    "      url: http://127.0.0.1:9/v1\n"
    "      api_key: stub-key\n"
)


def rss_mib(pid: int) -> float:
    """``pid``'s resident set size, in MiB: ``/proc`` where there is one, else ``ps``."""
    status = Path(f"/proc/{pid}/status")
    if status.exists():
        for line in status.read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
        return 0.0
    out = subprocess.run(  # noqa: S603 — a fixed argv, no shell
        ["ps", "-o", "rss=", "-p", str(pid)],  # noqa: S607 — ps from PATH, as on every POSIX
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return int(out) / 1024 if out else 0.0


@dataclass(frozen=True)
class Startup:
    """One backend's start: how long to its ready line, and its RSS after it."""

    ready_seconds: float
    samples: tuple[float, ...]

    @property
    def peak(self) -> float:
        return max(self.samples)


def start_and_measure(tmp_path: Path, name: str, *flags: str) -> Startup:
    """Start ``python -m igni_core.backend`` as the shell does, and sample it once ready."""
    home = tmp_path / f"{name}-home"
    project = tmp_path / f"{name}-project"
    (home / ".igni").mkdir(parents=True)
    project.mkdir()
    (home / ".igni" / "config.yaml").write_text(CONFIG)
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "IGNI_WS_TOKEN": f"memory-{name}",
        "IGNI_PARENT_PID": str(os.getpid()),
    }
    began = time.monotonic()
    child = subprocess.Popen(  # noqa: S603 — this interpreter, a fixed argv
        [sys.executable, "-m", "igni_core.backend", "--ws-port", "0", "--project-dir", str(project)]
        + list(flags),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert child.stdout is not None
        while True:
            line = child.stdout.readline()
            assert line, f"the {name} backend exited before its ready line ({child.wait()})"
            assert time.monotonic() - began < READY_WITHIN, f"the {name} backend never got ready"
            if line.lstrip().startswith("{") and json.loads(line).get("status") == "ready":
                break
        ready = time.monotonic() - began
        samples = []
        for _ in range(int(SAMPLE_FOR / SAMPLE_EVERY)):
            assert child.poll() is None, f"the {name} backend exited after its ready line"
            samples.append(rss_mib(child.pid))
            time.sleep(SAMPLE_EVERY)
        return Startup(ready, tuple(samples))
    finally:
        child.kill()
        child.wait(30)


@pytest.mark.skipif(sys.platform == "win32", reason="RSS is read from /proc or ps")
def test_the_account_only_backend_starts_no_heavier_than_the_full_one(
    tmp_path: Path, record_property: pytest.RecordProperty, request: pytest.FixtureRequest
) -> None:
    full = start_and_measure(tmp_path, "full")
    account_only = start_and_measure(tmp_path, "account-only", "--account-only")

    record = (
        f"startup RSS: account-only {account_only.peak:.1f} MiB "
        f"(ready in {account_only.ready_seconds:.1f} s), "
        f"full {full.peak:.1f} MiB (ready in {full.ready_seconds:.1f} s), "
        f"bound {ACCOUNT_ONLY_BOUND_MIB:.0f} MiB"
    )
    record_property("account_only_startup_rss_mib", round(account_only.peak, 1))
    record_property("full_startup_rss_mib", round(full.peak, 1))
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(f"\n{request.node.nodeid}: {record}")
    print(record)

    assert all(s > 0 for s in account_only.samples + full.samples), record
    assert account_only.peak <= full.peak * (1 + NOISE), record
    assert account_only.peak < ACCOUNT_ONLY_BOUND_MIB, record
