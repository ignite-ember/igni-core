"""``protocol.messages`` is the schemas package under its old name."""

from __future__ import annotations

from igni_core.protocol import messages, schemas


def test_the_shim_exports_exactly_the_schemas_names():
    assert messages.__all__ == list(schemas.__all__)


def test_every_exported_name_is_the_same_object_in_both():
    for name in schemas.__all__:
        assert getattr(messages, name) is getattr(schemas, name), name


def test_the_shim_is_not_a_copy_of_the_list():
    assert messages.__all__ is not schemas.__all__
