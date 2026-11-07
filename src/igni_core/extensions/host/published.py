"""The host schema igni-core publishes: ``docs/host-schema.json``.

``docs/HOST_OBJECT_MODEL.md``: "One schema, two SDKs." The schema is
:data:`~igni_core.extensions.host.definition.HOST_SCHEMA`. Both SDKs —
the Python SDK's ``igni_extension.host_model`` and the bridge's
``hostModel.generated.ts`` — live in igni-sdk and are generated there
from a copy of this file, so the schema crosses the repository line as
data, never as an import.

``tests/test_the_host_schema_is_published.py`` fails when the file
differs from the schema. Run ``make contracts`` after changing it, then
refresh igni-sdk's copy.
"""

from __future__ import annotations

from pathlib import Path

from igni_core.extensions.host.definition import HOST_SCHEMA

__all__ = ["PUBLISHED", "render", "write"]

#: Where igni-core publishes the schema, relative to nothing: absolute.
PUBLISHED = Path(__file__).resolve().parents[4] / "docs" / "host-schema.json"


def render() -> str:
    """The schema as igni-core publishes it."""
    return HOST_SCHEMA.model_dump_json(indent=2) + "\n"


def write() -> Path:
    """Write :data:`PUBLISHED`; where it went."""
    PUBLISHED.write_text(render())
    return PUBLISHED
