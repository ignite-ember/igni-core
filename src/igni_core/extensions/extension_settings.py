"""One extension's settings: what its manifest declares, and what was stored.

``contributes.settings`` describes configuration the host renders and
the deployment stores. Beside the bundle rather than inside it: the
bundle is what was signed, and a value an administrator typed is not
part of what a vendor attested to.
"""

from __future__ import annotations

from igni_core.extensions.manifest import ExtensionManifest, Setting
from igni_core.extensions.settings_values import SettingsValues
from igni_core.extensions.store_errors import StoreError


class ExtensionSettings:
    """The settings of one extension, as the host renders and stores them."""

    def __init__(self, manifest: ExtensionManifest, values: SettingsValues) -> None:
        self._manifest = manifest
        self._values = values

    @property
    def declared(self) -> dict[str, Setting]:
        """The settings the manifest declares, by key."""
        return {setting.key: setting for setting in self._manifest.contributes.settings}

    def read(self) -> dict[str, object]:
        """This extension's settings, defaults filled in.

        A key the manifest no longer declares is dropped rather than
        passed through: an extension that removed a setting should
        stop being handed it, and a stored value for a key nobody
        declares is how a stale option survives three upgrades.
        """
        declared = self.declared
        if not declared:
            return {}

        stored = self._values.read(self._manifest.id)

        return {
            key: stored.get(key, setting.default)
            for key, setting in declared.items()
            if stored.get(key, setting.default) is not None
        }

    def write(self, values: dict[str, object]) -> dict[str, object]:
        """Store values for the keys this extension declares.

        Undeclared keys are refused rather than ignored. A settings
        write is an administrator acting on what the host showed
        them, so a key that was not on that form did not come from
        there.
        """
        declared = self.declared
        unknown = sorted(set(values) - set(declared))
        if unknown:
            raise StoreError(
                f"{self._manifest.id} does not declare {unknown}. Its settings are "
                f"{sorted(declared) or 'none'}."
            )
        for key, setting in declared.items():
            if key in values and not setting.accepts(values[key]):
                raise StoreError(f"{self._manifest.id}: {key!r} must be one of {setting.options}")

        merged = {**self.read(), **values}
        self._values.write(self._manifest.id, merged)
        return merged


__all__ = ["ExtensionSettings"]
