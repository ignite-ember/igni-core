"""igni-core publishes the host schema both SDKs are generated from.

The SDKs live in igni-sdk, which generates them from its copy of
``docs/host-schema.json``. So that file is the schema, written down: it
must be what :data:`HOST_SCHEMA` serialises to, and read back it must be
the same schema — a field the JSON loses is a member an SDK never gets.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.extensions.host import published
from igni_core.extensions.host.definition import HOST_SCHEMA, SCHEMA_VERSION
from igni_core.extensions.host.spec import HostSchema


def test_the_published_file_is_the_schema() -> None:
    assert published.PUBLISHED.read_text() == published.render(), (
        "docs/host-schema.json is older than the host schema. Run `make contracts`, "
        "then refresh igni-sdk's copy."
    )


def test_it_reads_back_as_the_same_schema() -> None:
    assert HostSchema.model_validate_json(published.PUBLISHED.read_text()) == HOST_SCHEMA


def test_it_carries_the_schema_version() -> None:
    assert HostSchema.model_validate_json(published.render()).version == SCHEMA_VERSION


def test_it_is_rendered_as_indented_json_with_a_trailing_newline() -> None:
    rendered = published.render()

    assert rendered.endswith("}\n") and not rendered.endswith("\n\n")
    assert rendered.startswith('{\n  "')


def test_it_is_published_in_docs_of_this_repository() -> None:
    assert published.PUBLISHED.is_absolute()
    assert published.PUBLISHED.parts[-2:] == ("docs", "host-schema.json")
    assert published.PUBLISHED.parent.is_dir()


def test_writing_it_puts_the_rendered_schema_where_it_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "host-schema.json"
    monkeypatch.setattr(published, "PUBLISHED", target)

    assert published.write() == target

    assert target.read_text() == published.render()
