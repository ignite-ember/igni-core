"""The trust store: the file that holds the person's decisions.

The file is read on every decision, because ``igni trust`` in a terminal
and the desktop's backend are different processes, and a revocation in one
has to reach the other. It is also the one file a folder must not be able
to write for itself, so it lives in the user's data directory and never in
the repository being trusted.

A file that cannot be read — unreadable, not JSON, or not the shape the
store writes — trusts nothing. That is the whole point of the failure
mode: a broken file must not fall open.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from igni_core.core.workspace_trust import (
    WorkspaceIdentity,
    WorkspaceTrustStore,
)

pytestmark = pytest.mark.workspace_trust


def _store(tmp_path: Path) -> tuple[WorkspaceTrustStore, Path]:
    path = tmp_path / "trust.json"
    return WorkspaceTrustStore(path=path), path


def _identity(name: str = "folder") -> WorkspaceIdentity:
    return WorkspaceIdentity(path=f"/tmp/{name}", remote=None)


def test_a_decision_is_remembered_and_forgotten(tmp_path):
    store, path = _store(tmp_path)
    identity = _identity()

    store.set(identity, "trusted")

    assert store.get(identity) == "trusted"
    assert path.is_file()
    assert [entry.path for entry in store.entries()] == [identity.path]

    assert store.forget(identity) is True

    assert store.get(identity) is None
    assert store.entries() == []


def test_forgetting_a_folder_with_no_decision_says_so(tmp_path):
    store, _ = _store(tmp_path)

    assert store.forget(_identity("never-heard-of-it")) is False


def test_a_second_decision_replaces_the_first(tmp_path):
    store, _ = _store(tmp_path)
    identity = _identity()

    store.set(identity, "trusted")
    store.set(identity, "untrusted")

    assert store.get(identity) == "untrusted"
    assert len(store.entries()) == 1


def test_a_file_that_cannot_be_read_leaves_every_folder_undecided(tmp_path):
    """A trust file that is a directory cannot be read; nothing is trusted."""
    store, path = _store(tmp_path)
    path.mkdir(parents=True)

    assert store.get(_identity()) is None
    assert store.entries() == []


def test_a_file_that_is_not_json_trusts_nothing(tmp_path):
    store, path = _store(tmp_path)
    path.write_text("this is not json", encoding="utf-8")

    assert store.get(_identity()) is None


def test_a_file_of_the_wrong_shape_trusts_nothing(tmp_path):
    """The file the store writes has entries with a path and a state; a file
    of another shape is not read as one."""
    store, path = _store(tmp_path)
    path.write_text(
        json.dumps({"version": 1, "workspaces": [{"path": 7, "state": "trusted"}]}),
        encoding="utf-8",
    )

    assert store.get(_identity()) is None


def test_an_entry_knows_the_identity_it_was_recorded_for(tmp_path):
    store, _ = _store(tmp_path)
    identity = _identity()

    store.set(identity, "trusted")

    entry = store.entries()[0]
    assert entry.identity == identity
    assert entry.state == "trusted"
    assert entry.decided_at


def test_a_file_that_is_not_text_trusts_nothing(tmp_path):
    """Bytes that are not UTF-8 are a broken file, not a crash: nothing is
    trusted, and the next decision rewrites the file."""
    store, path = _store(tmp_path)
    path.write_bytes(b"\xff\xfe\x00 not utf-8 \x80")

    assert store.get(_identity()) is None
    assert store.entries() == []

    store.set(_identity(), "trusted")

    assert store.get(_identity()) == "trusted"
