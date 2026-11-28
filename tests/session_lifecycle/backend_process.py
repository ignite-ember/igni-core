"""The real backend, as the desktop app starts it: ``python -m igni_core.backend --ws-port 0``."""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from igni_core.backend.schemas_rpc import BackendReadyLine as BackendReady
from tests.session_lifecycle.constants import SHARED_HF_CACHE


class BackendProcess:
    """One ``igni_core.backend`` process over a temp home and project."""

    READY_TIMEOUT_SECONDS = 90.0

    def __init__(
        self, home: Path, project: Path, model_url: str, model_id: str, mutant: str = ""
    ) -> None:
        self.home = home
        # A bug put back in (``mutant_backend.Mutants``); empty for the real backend.
        self.mutant = mutant
        self.project = project
        self.model_url = model_url
        self.model_id = model_id
        self.token = secrets.token_urlsafe(24)
        self.log_path = home / "backend-stderr.log"
        self.ready: BackendReady | None = None
        self.starts = 0
        self._proc: subprocess.Popen[bytes] | None = None
        self._stdout_lines: list[str] = []

    @property
    def url(self) -> str:
        assert self.ready is not None, "the backend is not running"
        return f"ws://127.0.0.1:{self.ready.ws_port}"

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    def configure(self, extra_yaml: str = "", models_yaml: str | None = None) -> None:
        """Write ``~/.igni/config.yaml``: the scripted model, or ``models_yaml``."""
        config = self.home / ".igni" / "config.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        if models_yaml is None:
            models_yaml = "air_gapped: true\n" + self._scripted_models()
        config.write_text("update_check_ttl: 0\n" + models_yaml + extra_yaml)
        os.chmod(config, 0o600)
        (self.project / ".igni").mkdir(parents=True, exist_ok=True)

    def _scripted_models(self) -> str:
        entries = "".join(
            f"    {name}:\n"
            "      provider: openai_like\n"
            f"      model_id: {model_id}\n"
            f"      url: {self.model_url}\n"
            "      api_key: scripted-key\n"
            "      context_window: 200000\n"
            for name, model_id in (
                ("scripted", self.model_id),
                ("scripted-alt", f"{self.model_id}-alt"),
            )
        )
        return "models:\n  default: scripted\n  registry:\n" + entries

    def environment(self) -> dict[str, str]:
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("IGNI_", "OPENAI_", "ANTHROPIC_")) and k != "PYTHONPATH"
        }
        repo = Path(__file__).resolve().parents[2]
        # Another checkout's ``src`` (a fix branch's) runs under this harness when named.
        core_src = os.environ.get("IGNI_LIFECYCLE_CORE_SRC") or str(repo / "src")
        env.update(
            HOME=str(self.home),
            IGNI_WS_TOKEN=self.token,
            AGNO_TELEMETRY="false",
            PYTHONPATH=os.pathsep.join([core_src, str(repo)]),
            PYTHONUNBUFFERED="1",
            # One model cache for every backend the harness starts: the decisions
            # server's download lands once, not once per temp home.
            HF_HOME=str(SHARED_HF_CACHE),
            HF_HUB_CACHE=str(SHARED_HF_CACHE / "hub"),
        )
        return env

    def start(self) -> BackendReady:
        """Start the process and wait for its ready line."""
        assert self._proc is None or self._proc.poll() is not None, "already running"
        self.starts += 1
        log = self.log_path.open("ab")
        entry = (
            ["tests.session_lifecycle.mutant_backend", self.mutant]
            if self.mutant
            else ["igni_core.backend"]
        )
        self._proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                *entry,
                "--ws-port",
                "0",
                "--project-dir",
                str(self.project),
            ],
            cwd=str(self.project),
            env=self.environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=log,
            start_new_session=True,
        )
        log.close()
        self.ready = self._read_ready(self._proc)
        return self.ready

    def _read_ready(self, proc: subprocess.Popen[bytes]) -> BackendReady:
        assert proc.stdout is not None
        found: list[BackendReady] = []
        done = threading.Event()

        def read() -> None:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                self._stdout_lines.append(line)
                if not found and line.startswith("{"):
                    try:
                        payload = json.loads(line)
                    except ValueError:
                        continue
                    if payload.get("status") == "ready":
                        found.append(BackendReady.model_validate(payload))
                        done.set()
            done.set()

        threading.Thread(target=read, daemon=True).start()
        done.wait(self.READY_TIMEOUT_SECONDS)
        if not found:
            self.kill()
            raise RuntimeError(
                f"the backend never said it was ready; stderr tail:\n{self.stderr_tail()}"
            )
        return found[0]

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self, timeout: float = 30.0) -> int | None:
        """SIGTERM, as quitting the app does; SIGKILL if it does not go; then its leftovers."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return proc.returncode if proc is not None else None
        family = self._descendants(proc.pid)
        proc.send_signal(signal.SIGTERM)
        try:
            code: int | None = proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            code = None
        self._reap(family)
        return code

    def kill(self) -> None:
        """SIGKILL the whole process group and every descendant: a crash, nothing flushed."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        family = self._descendants(proc.pid)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(10)
        self._reap(family)

    @staticmethod
    def _descendants(pid: int) -> list[int]:
        """Every process under ``pid`` (the decisions server, shells), even in other groups."""
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, check=False
        ).stdout
        children: dict[int, list[int]] = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2:
                children.setdefault(int(parts[1]), []).append(int(parts[0]))
        found: list[int] = []
        todo = [pid]
        while todo:
            for child in children.get(todo.pop(), []):
                found.append(child)
                todo.append(child)
        return found

    @staticmethod
    def _reap(pids: list[int]) -> None:
        for pid in pids:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)

    def restart(self, graceful: bool = True) -> BackendReady:
        """Stop (or kill) and start again on the same home and project."""
        if graceful:
            self.stop(timeout=10.0)
        else:
            self.kill()
        time.sleep(0.2)
        return self.start()

    def stderr_tail(self, lines: int = 60) -> str:
        try:
            text = self.log_path.read_text(errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])

    def state_db(self) -> Path:
        """The project's ``state.db`` under the temp home."""
        found = sorted((self.home / ".igni" / "projects").glob("*/state.db"))
        assert found, "the backend has written no state.db"
        return found[0]
