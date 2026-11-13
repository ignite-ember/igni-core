"""``HostModelConfig``: the inputs a host model is built from, and their defaults.

Production leaves the optional collaborators out and the answer check off.
"""

from __future__ import annotations

from igni_core.extensions.host.definition import HOST_SCHEMA
from igni_core.extensions.host.model_config import HostModelConfig
from igni_core.extensions.registry import ExtensionRegistry


class Backend:
    """A stand-in the config holds without looking inside."""


class TestHostModelConfig:
    def test_what_is_left_out_is_none_and_the_answers_are_not_checked(self) -> None:
        config = HostModelConfig(backend=Backend(), registry=ExtensionRegistry())  # type: ignore[arg-type]
        assert config.store is None
        assert config.notices is None
        assert config.decisions is None
        assert config.strict is False
        assert config.host_schema is HOST_SCHEMA

    def test_collaborators_are_held_as_given_not_copied_or_validated(self) -> None:
        backend, registry = Backend(), ExtensionRegistry()
        config = HostModelConfig(backend=backend, registry=registry, strict=True)  # type: ignore[arg-type]
        assert config.backend is backend
        assert config.registry is registry
        assert config.strict is True
