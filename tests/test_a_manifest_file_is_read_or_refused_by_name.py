"""``ExtensionManifest.load`` reads one file, and any refusal names it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from igni_core.extensions.manifest import ExtensionManifest, ManifestError

GOOD = {
    "id": "acme.tickets",
    "version": "0.1.0",
    "contract": ">=1.0 <2",
    "trust": "unsigned",
    "dev": {"command": ["python", "server.py"]},
}


def _write(tmp_path: Path, content: str | bytes) -> Path:
    path = tmp_path / "manifest.json"
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def test_a_valid_file_loads_into_the_model(tmp_path):
    manifest = ExtensionManifest.load(_write(tmp_path, json.dumps(GOOD)))
    assert manifest.id == "acme.tickets"
    assert manifest.packaging == "dev"


def test_text_that_is_not_json_is_refused_naming_the_file(tmp_path):
    path = _write(tmp_path, "{not json")
    with pytest.raises(ManifestError) as caught:
        ExtensionManifest.load(path)
    assert str(caught.value).startswith(f"{path}: not valid JSON")


@pytest.mark.parametrize(
    ("text", "kind"), [("[1, 2]", "list"), ('"x"', "str"), ("null", "NoneType"), ("3", "int")]
)
def test_a_root_that_is_not_an_object_says_what_it_was(tmp_path, text, kind):
    path = _write(tmp_path, text)
    with pytest.raises(ManifestError) as caught:
        ExtensionManifest.load(path)
    assert str(caught.value) == f"{path}: a manifest is a JSON object, got {kind}"


def test_bytes_that_are_not_utf8_are_refused_naming_the_file(tmp_path):
    path = _write(tmp_path, b'{"id": "\xff\xfe"}')
    with pytest.raises(ManifestError) as caught:
        ExtensionManifest.load(path)
    assert str(caught.value).startswith(f"{path}: not UTF-8 text")


def test_a_missing_file_is_not_hidden_as_a_manifest_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        ExtensionManifest.load(tmp_path / "manifest.json")
