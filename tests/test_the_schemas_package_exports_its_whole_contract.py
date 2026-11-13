"""``igni_core.protocol.schemas`` lists, in ``__all__``, every name it imports for the contract."""

from __future__ import annotations

import pkgutil

from pydantic import BaseModel

from igni_core.protocol import schemas


def test_every_listed_name_is_importable_and_listed_once():
    assert len(schemas.__all__) == len(set(schemas.__all__))
    for name in schemas.__all__:
        assert hasattr(schemas, name), name


def test_every_name_the_package_imports_is_listed():
    imported = {
        name
        for name, obj in vars(schemas).items()
        if not name.startswith("_")
        and isinstance(obj, type)
        and obj.__module__.startswith(schemas.__name__ + ".")
    }
    assert imported - set(schemas.__all__) == set()


def test_star_import_brings_exactly_the_listed_names():
    namespace: dict[str, object] = {}
    exec("from igni_core.protocol.schemas import *", namespace)  # noqa: S102
    namespace.pop("__builtins__")
    assert set(namespace) == set(schemas.__all__)


def test_every_model_a_schema_module_defines_publicly_is_exported():
    listed = {getattr(schemas, n) for n in schemas.__all__}
    missing = []
    for info in pkgutil.iter_modules(schemas.__path__):
        module = __import__(f"{schemas.__name__}.{info.name}", fromlist=["x"])
        for name, obj in vars(module).items():
            if (
                isinstance(obj, type)
                and issubclass(obj, BaseModel)
                and obj.__module__ == module.__name__
                and obj not in listed
                and not name.startswith("_")
            ):
                missing.append(f"{info.name}.{name}")
    assert missing == [], missing


def test_the_event_and_enum_names_are_the_ones_the_submodules_define():
    from igni_core.protocol.schemas import be_events, enums

    for name in be_events.__all__:
        assert getattr(schemas, name) is getattr(be_events, name)
    for name in enums.__all__:
        assert getattr(schemas, name) is getattr(enums, name)
