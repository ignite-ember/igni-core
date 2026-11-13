"""A checksums file that is valid JSON but not an object reads as empty.

``.igni/.checksums.json`` is igni's own bookkeeping, but it sits in a folder a
person can edit and a merge can mangle. A list, a string or ``null`` parsed
fine and then crashed the first run of every session in the project with a
``ValueError`` out of ``dict(...)``; an entry whose value was not text failed
validation the same way. The file is rebuilt from the bundle, so the right
reading is an empty store.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from igni_core.core.init.checksum_store import ChecksumStore
from igni_core.core.init.json_file import JsonFile
from igni_core.core.init.schemas import InitConfig
from igni_core.core.paths import CONFIG_DIR


def _write(project: Path, text: str) -> None:
    path = project / CONFIG_DIR / ".checksums.json"
    path.parent.mkdir(parents=True)
    path.write_text(text)


@pytest.mark.parametrize("text", ["[]", '["agents/x.md"]', '"agents/x.md"', "null", "7"])
def test_json_that_is_not_an_object_loads_as_an_empty_dict(tmp_path: Path, text: str):
    path = tmp_path / "f.json"
    path.write_text(text)

    assert JsonFile(path=path).load() == {}


@pytest.mark.parametrize("text", ["[]", '["agents/x.md"]', '"agents/x.md"', "null"])
def test_the_store_starts_empty_from_a_checksums_file_that_is_not_an_object(
    tmp_path: Path, text: str
):
    _write(tmp_path, text)

    store = ChecksumStore.load(tmp_path, InitConfig())

    assert store.entries == {}


def test_an_entry_that_is_not_text_is_dropped_and_the_rest_kept(tmp_path: Path):
    _write(tmp_path, json.dumps({"agents/x.md": "abc", "agents/y.md": 12, "skills/z": None}))

    store = ChecksumStore.load(tmp_path, InitConfig())

    assert store.entries == {"agents/x.md": "abc"}
