"""Finds the workflows a session can run and reads each one's ``meta``."""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel

from igni_core.backend.schemas_workflows import WorkflowMetaEnvelope
from igni_core.backend.workflow_layers import FallbackLayerNotice, WorkflowLayers
from igni_core.backend.workflow_runtime_process import RUNTIME_PATH, WorkflowRuntime
from igni_core.core.workspace_trust import WorkspaceTrust

logger = logging.getLogger(__name__)


class CachedMeta(BaseModel):
    mtime: float
    envelope: WorkflowMetaEnvelope


class MetaCache:
    """Each file's meta, kept until the file's mtime changes."""

    def __init__(self) -> None:
        self._entries: dict[Path, CachedMeta] = {}

    def get(self, path: Path, mtime: float) -> WorkflowMetaEnvelope | None:
        cached = self._entries.get(path)
        return cached.envelope if cached is not None and cached.mtime == mtime else None

    def put(self, path: Path, mtime: float, envelope: WorkflowMetaEnvelope) -> None:
        self._entries[path] = CachedMeta(mtime=mtime, envelope=envelope)


class WorkflowDiscovery:
    """The workflows across :class:`WorkflowLayers`, one per name.

    A folder's own workflows are neither discovered nor run until the
    folder is trusted (``core/workspace_trust``).
    """

    def __init__(
        self,
        *,
        layers: WorkflowLayers,
        runtime: WorkflowRuntime,
        trust: WorkspaceTrust,
        notice: FallbackLayerNotice,
    ) -> None:
        self._layers = layers
        self._runtime = runtime
        self._trust = trust
        self._notice = notice
        self._cache = MetaCache()

    @classmethod
    def for_project(
        cls,
        project_dir: Path,
        *,
        group_dir: Path | None = None,
        home_dir: Path | None = None,
        trust: WorkspaceTrust | None = None,
        notice: FallbackLayerNotice | None = None,
        runtime: WorkflowRuntime | None = None,
    ) -> WorkflowDiscovery:
        layers = WorkflowLayers.for_project(project_dir, group_dir=group_dir, home_dir=home_dir)
        return cls(
            layers=layers,
            runtime=runtime or WorkflowRuntime(runtime_path=RUNTIME_PATH, cwd=layers.project_dir),
            trust=trust or WorkspaceTrust.shared(),
            notice=notice or FallbackLayerNotice(),
        )

    @property
    def layers(self) -> WorkflowLayers:
        return self._layers

    def _trusted(self) -> bool:
        return self._trust.is_trusted(self._layers.project_dir, what="workflows")

    def paths(self) -> list[Path]:
        """Every workflow file this session may read, in shadowing order."""
        return self._layers.paths(trusted=self._trusted(), notice=self._notice)

    def not_found_message(self, name: str) -> str:
        return self._layers.not_found_message(name, trusted=self._trusted())

    async def list_workflows(self) -> list[WorkflowMetaEnvelope]:
        """Every workflow's meta, one per name, sorted by name."""
        shadowed = WorkflowLayers.shadow(self.paths())
        results: list[WorkflowMetaEnvelope] = []
        for name in sorted(shadowed):
            envelope = await self._meta_for(shadowed[name])
            if envelope is not None:
                results.append(envelope)
        return results

    async def resolve(self, name: str) -> WorkflowMetaEnvelope | None:
        """The meta of the copy of ``name`` that wins, if any layer has one."""
        for path in reversed(self.paths()):
            if path.stem == name:
                return await self._meta_for(path)
        return None

    async def _meta_for(self, path: Path) -> WorkflowMetaEnvelope | None:
        mtime = path.stat().st_mtime
        cached = self._cache.get(path, mtime)
        if cached is not None:
            return cached
        result = await self._runtime.probe_meta(path)
        if result.meta is None:
            logger.debug("discovery failed for %s: %s", path, result.reason)
            return None
        envelope = WorkflowMetaEnvelope(meta=result.meta, path=self._layers.display_path(path))
        self._cache.put(path, mtime, envelope)
        return envelope
