"""igni-core publishes the host schema both SDKs are generated from.

The SDKs live in igni-sdk, which generates them from its copy of
``docs/host-schema.json``. So that file is the schema, written down: it
must be what :data:`HOST_SCHEMA` serialises to, and read back it must be
the same schema — a field the JSON loses is a member an SDK never gets.
"""

from __future__ import annotations

from ember_code.extensions.host import published
from ember_code.extensions.host.definition import HOST_SCHEMA, SCHEMA_VERSION
from ember_code.extensions.host.spec import HostSchema


def test_the_published_file_is_the_schema() -> None:
    assert published.PUBLISHED.read_text() == published.render(), (
        "docs/host-schema.json is older than the host schema. Run `make contracts`, "
        "then refresh igni-sdk's copy."
    )


def test_it_reads_back_as_the_same_schema() -> None:
    assert HostSchema.model_validate_json(published.PUBLISHED.read_text()) == HOST_SCHEMA


def test_it_carries_the_schema_version() -> None:
    assert HostSchema.model_validate_json(published.render()).version == SCHEMA_VERSION
