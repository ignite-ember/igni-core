"""The whole system under test, put together: the scripted model, the backend, windows."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from tests.session_lifecycle.backend_process import BackendProcess
from tests.session_lifecycle.lifecycle_client import LifecycleClient
from tests.session_lifecycle.model_script import ModelScript
from tests.session_lifecycle.scripted_model_server import ScriptedModelServer
from tests.session_lifecycle.state_db import StateDb

# The rig's permissions: the sleeps the tool and background states run are allowed,
RIG_CONFIG = (
    "permissions:\n"
    "  allow:\n"
    '    - "Bash(sleep *)"\n'
    "safety:\n"
    "  bypass_guard:\n"
    "    use_model: false\n"
)

# Planted in every secret file of the rig; no request to the model may contain it.
SECRET_CANARY = "IGNI-CANARY-7f3a9c1e-do-not-send"


class LifecycleRig:
    """A temp home and project, the scripted model, the real backend, and its windows."""

    def __init__(
        self,
        root: Path,
        script: ModelScript | None = None,
        extra_config: str = "",
        models_yaml: str | None = None,
        mutant: str = "",
    ) -> None:
        self.root = root
        self.home = root / "home"
        self.project = root / "project"
        self.model = ScriptedModelServer(script)
        self.extra_config = extra_config
        # A real model's configuration instead of the scripted one (the live tier).
        self.models_yaml = models_yaml
        self.mutant = mutant
        self.backend: BackendProcess | None = None
        self.windows: list[LifecycleClient] = []

    @property
    def script(self) -> ModelScript:
        return self.model.script

    def start(self) -> LifecycleRig:
        self.home.mkdir(parents=True, exist_ok=True)
        self.project.mkdir(parents=True, exist_ok=True)
        self._plant_secrets()
        (self.project / "README.md").write_text("# A project for the lifecycle tests\n")
        self.model.start()
        self.backend = BackendProcess(
            self.home, self.project, self.model.url, self.model.model_id, self.mutant
        )
        self.backend.configure(self.extra_config, self.models_yaml)
        self.backend.start()
        return self

    def _plant_secrets(self) -> None:
        secrets = [
            self.project / ".env",
            self.home / ".aws" / "credentials",
            self.home / ".ssh" / "id_ed25519",
            self.home / ".igni" / ".env",
        ]
        for path in secrets:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"SECRET_TOKEN={SECRET_CANARY}\n")
            os.chmod(path, 0o600)

    async def window(self, name: str = "window") -> LifecycleClient:
        """A new window connected to the backend (not yet bound to a session)."""
        assert self.backend is not None
        client = await LifecycleClient(self.backend.url, self.backend.token, name).connect()
        self.windows.append(client)
        return client

    async def new_session(self, name: str = "window") -> LifecycleClient:
        """A window bound to a new session in the project, as a window opened on a folder is."""
        client = await self.window(name)
        result = await client.rpc("attach_session", {"project_dir": str(self.project)})
        client.session_id = str(result["session_id"])
        client.view.rebind(client.session_id)
        return client

    def restart(self, graceful: bool = True) -> None:
        """Restart the backend on the same home and project; windows must reconnect."""
        assert self.backend is not None
        self.backend.restart(graceful=graceful)
        for window in self.windows:
            window.url = self.backend.url

    @property
    def db(self) -> StateDb:
        assert self.backend is not None
        return StateDb(self.backend.state_db())

    async def close_windows(self) -> None:
        for window in self.windows:
            await window.close()
        self.windows = []

    def stop(self) -> None:
        """Stop the backend and everything it started, then delete the temp home and project."""
        if self.backend is not None:
            self.backend.stop(timeout=10.0)
        self.model.stop()
        for path in (self.home, self.project):
            shutil.rmtree(path, ignore_errors=True)
