"""``scripts/reindex_hnsw.py`` rebuilds chroma collections with the high-recall HNSW config.

The script copies each collection into a temporary one with the target
metadata, drops the original, recreates it and copies back, moving the
stored vectors verbatim. It imports the chroma client factory, the index
path helpers and the embedding function from modules this repository no
longer carries, and ``chromadb`` is not a dependency, so the tests stand
those in: ``chromadb.PersistentClient`` answers with an in-memory store
keyed by directory, the path helpers use the documented layout
(``<data>/projects/<id>/code_index/<sha>.chroma`` and
``<data>/projects/<id>/knowledge.chroma``), and the embedding function is
a recorder. The script's own copy, swap, verify, walk and exit-code logic
runs unmodified.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest

_SOURCE = pathlib.Path(__file__).resolve().parent.parent / "scripts/reindex_hnsw.py"

_RUNTIME_METADATA = {"hnsw:space": "cosine", "hnsw:M": 32, "hnsw:search_ef": 10000}


class _Collection:
    def __init__(self, name, metadata, ef):
        self.name = name
        self.metadata = metadata
        self.embedding_function = ef
        self.rows: dict[str, tuple[list[float], str, dict]] = {}
        self.pages: list[int] = []
        self.drop_after_adds: int | None = None

    def count(self):
        return len(self.rows)

    def get(self, limit, offset, include):
        assert set(include) == {"embeddings", "documents", "metadatas"}
        self.pages.append(limit)
        ids = list(self.rows)[offset : offset + limit]
        return {
            "ids": ids,
            "embeddings": [self.rows[i][0] for i in ids],
            "documents": [self.rows[i][1] for i in ids],
            "metadatas": [self.rows[i][2] for i in ids],
        }

    def add(self, ids, embeddings, documents, metadatas):
        for i, e, d, m in zip(ids, embeddings, documents, metadatas, strict=True):
            self.rows[i] = (e, d, m)
        if self.drop_after_adds is not None:
            # a store that silently loses rows, so the count check has something to find
            for i in list(self.rows)[self.drop_after_adds :]:
                del self.rows[i]


class _Client:
    def __init__(self):
        self.collections: dict[str, _Collection] = {}
        self.log: list[tuple[str, str]] = []
        self.lose_rows_in: str | None = None

    def get_collection(self, name, embedding_function):
        if name not in self.collections:
            raise ValueError(f"Collection {name} does not exist.")
        return self.collections[name]

    def create_collection(self, name, embedding_function, metadata):
        self.log.append(("create", name))
        self.collections[name] = _Collection(name, dict(metadata), embedding_function)
        if name == self.lose_rows_in:
            self.collections[name].drop_after_adds = 1
        return self.collections[name]

    def delete_collection(self, name):
        self.log.append(("delete", name))
        if name not in self.collections:
            raise ValueError(f"Collection {name} does not exist.")
        del self.collections[name]

    def seed(self, name, n, metadata=None):
        coll = _Collection(name, metadata or {"hnsw:space": "l2"}, "old-ef")
        for i in range(n):
            coll.rows[f"id{i:04d}"] = ([float(i), 0.5], f"doc {i} é", {"n": i})
        self.collections[name] = coll
        return coll


@pytest.fixture
def hnsw(tmp_path, monkeypatch):
    stores: dict[str, _Client] = {}
    embedding_instances = []

    chromadb = types.ModuleType("chromadb")
    chromadb.PersistentClient = lambda path: stores.setdefault(str(path), _Client())

    factory = types.ModuleType("igni_core.core.code_index.chroma_client_factory")
    factory.ChromaClientFactory = type("ChromaClientFactory", (), {"HNSW_METADATA": _RUNTIME_METADATA})

    def commit_chroma_path(project, sha, data_dir):
        return data_dir / "projects" / "proj-id" / "code_index" / f"{sha}.chroma"

    def knowledge_chroma_path(project, data_dir):
        return data_dir / "projects" / "proj-id" / "knowledge.chroma"

    paths = types.ModuleType("igni_core.core.code_index.paths")
    paths.commit_chroma_path = commit_chroma_path
    paths.knowledge_chroma_path = knowledge_chroma_path

    embeddings = types.ModuleType("igni_core.core.embeddings")

    class EmbeddingFunction:
        def __init__(self):
            embedding_instances.append(self)

    embeddings.EmbeddingFunction = EmbeddingFunction

    package = types.ModuleType("igni_core.core.code_index")
    package.__path__ = []
    for name, module in {
        "chromadb": chromadb,
        "igni_core.core.code_index": package,
        "igni_core.core.code_index.chroma_client_factory": factory,
        "igni_core.core.code_index.paths": paths,
        "igni_core.core.embeddings": embeddings,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(sys, "path", list(sys.path))

    spec = importlib.util.spec_from_file_location("reindex_hnsw_under_test", _SOURCE)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)

    data = tmp_path / "data"
    project = tmp_path / "project"
    project.mkdir()

    class Env:
        pass

    env = Env()
    env.module, env.stores, env.data, env.project = module, stores, data, project
    env.embedding_instances = embedding_instances
    env.commit_dir = lambda sha: data / "projects/proj-id/code_index" / f"{sha}.chroma"
    env.knowledge_dir = data / "projects/proj-id/knowledge.chroma"
    env.client = lambda path: stores.setdefault(str(path), _Client())

    def make_dir(path):
        path.mkdir(parents=True, exist_ok=True)
        (path / "chroma.sqlite3").write_text("db")
        return path

    env.make_dir = make_dir

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["reindex_hnsw.py", *argv])
        return module.main()

    env.run = run
    return env


# -- the target metadata -----------------------------------------------------


def test_the_target_metadata_is_a_copy_of_what_the_runtime_uses(hnsw):
    assert hnsw.module.TARGET_HNSW_METADATA == _RUNTIME_METADATA
    assert hnsw.module.TARGET_HNSW_METADATA is not _RUNTIME_METADATA


# -- _copy_collection --------------------------------------------------------


def test_a_collection_is_rebuilt_with_the_target_metadata_and_every_row_unchanged(hnsw):
    client = _Client()
    old = client.seed("c", 3)
    before = dict(old.rows)

    n = hnsw.module._copy_collection(client, "c")

    rebuilt = client.collections["c"]
    assert n == 3
    assert rebuilt is not old
    assert rebuilt.metadata == _RUNTIME_METADATA
    assert rebuilt.rows == before
    assert rebuilt.embedding_function is hnsw.embedding_instances[0]


def test_the_temporary_collection_is_gone_afterwards_and_the_swap_order_is_fixed(hnsw):
    client = _Client()
    client.seed("c", 2)

    hnsw.module._copy_collection(client, "c")

    assert set(client.collections) == {"c"}
    assert client.log == [
        ("delete", "c__rehnsw_tmp"),
        ("create", "c__rehnsw_tmp"),
        ("delete", "c"),
        ("create", "c"),
        ("delete", "c__rehnsw_tmp"),
    ]


def test_a_leftover_temp_collection_from_an_earlier_run_is_replaced(hnsw):
    client = _Client()
    client.seed("c", 2)
    stale = client.seed("c__rehnsw_tmp", 9)

    n = hnsw.module._copy_collection(client, "c")

    assert n == 2
    assert stale.name == "c__rehnsw_tmp"
    assert client.collections["c"].count() == 2
    assert "c__rehnsw_tmp" not in client.collections


def test_an_empty_collection_is_skipped_and_left_untouched(hnsw, capsys):
    client = _Client()
    empty = client.seed("c", 0)

    n = hnsw.module._copy_collection(client, "c")

    assert n == 0
    assert "[c] empty, skipping" in capsys.readouterr().out
    assert client.collections["c"] is empty
    assert client.log == []


@pytest.mark.parametrize(
    "total, pages",
    [(1, 1), (500, 1), (501, 2), (1000, 2), (1001, 3)],
)
def test_rows_move_in_batches_of_500_and_none_is_lost_at_the_boundary(hnsw, total, pages):
    client = _Client()
    src = client.seed("c", total)

    n = hnsw.module._copy_collection(client, "c")

    assert n == total
    assert client.collections["c"].count() == total
    assert list(client.collections["c"].rows) == list(src.rows)
    assert len(src.pages) == pages
    assert set(src.pages) == {500}


def test_progress_is_printed_per_batch(hnsw, capsys):
    client = _Client()
    client.seed("c", 501)

    hnsw.module._copy_collection(client, "c")

    out = capsys.readouterr().out
    assert "[c] copied 500/501" in out
    assert "[c] copied 501/501" in out


def test_a_copy_that_lands_short_raises_naming_the_collection_before_touching_the_original(hnsw):
    client = _Client()
    original = client.seed("c", 3)
    client.lose_rows_in = "c__rehnsw_tmp"

    with pytest.raises(RuntimeError, match=r"copy verify failed for c: src=3 dst=1"):
        hnsw.module._copy_collection(client, "c")

    assert client.collections["c"] is original
    assert original.count() == 3


def test_a_swap_that_lands_short_raises_naming_the_collection(hnsw):
    client = _Client()
    client.seed("c", 3)
    # the second creation of "c" is the swap; lose its rows
    real_create = client.create_collection
    seen = []

    def create(name, embedding_function, metadata):
        coll = real_create(name, embedding_function, metadata)
        if name == "c":
            seen.append(coll)
            coll.drop_after_adds = 1
        return coll

    client.create_collection = create

    with pytest.raises(RuntimeError, match=r"swap verify failed for c: temp=3 final=1"):
        hnsw.module._copy_collection(client, "c")


# -- reindex_chroma_dir ------------------------------------------------------


def test_a_missing_collection_is_reported_as_not_present_and_counted_zero(hnsw, tmp_path, capsys):
    client = hnsw.client(tmp_path / "x.chroma")
    client.seed("a", 2)

    counts = hnsw.module.reindex_chroma_dir(tmp_path / "x.chroma", ("a", "b"))

    assert counts == (2, 0)
    assert "[b] not present, skipping" in capsys.readouterr().out
    assert client.collections["a"].metadata == _RUNTIME_METADATA


def test_the_counts_come_back_as_a_tuple_in_the_order_asked(hnsw, tmp_path):
    client = hnsw.client(tmp_path / "x.chroma")
    client.seed("a", 1)
    client.seed("b", 4)

    assert hnsw.module.reindex_chroma_dir(tmp_path / "x.chroma", ("b", "a")) == (4, 1)


# -- _reindex_code_index -----------------------------------------------------


def _seed_commit(hnsw, sha, docs, chunks):
    path = hnsw.make_dir(hnsw.commit_dir(sha))
    client = hnsw.client(path)
    client.seed("code_index_documents", docs)
    client.seed("code_index_chunks", chunks)
    return path, client


def test_every_commit_dir_is_rebuilt_in_sorted_order_and_the_items_are_summed(hnsw, capsys):
    _seed_commit(hnsw, "bbb", 2, 3)
    _, client_a = _seed_commit(hnsw, "aaa", 1, 1)

    dirs, items = hnsw.module._reindex_code_index(hnsw.project, hnsw.data, keep_backup=False)

    out = capsys.readouterr().out
    assert (dirs, items) == (2, 7)
    assert out.index("=== code_index aaa") < out.index("=== code_index bbb")
    assert "done: 1 docs, 1 chunks" in out
    assert client_a.collections["code_index_documents"].metadata == _RUNTIME_METADATA


def test_probe_dirs_and_non_chroma_entries_are_left_alone(hnsw):
    _seed_commit(hnsw, "aaa", 1, 1)
    probe, probe_client = _seed_commit(hnsw, "PROBE-x", 1, 1)
    (hnsw.commit_dir("aaa").parent / "notes.txt").write_text("x")
    (hnsw.commit_dir("aaa").parent / "old.bak").mkdir()

    dirs, items = hnsw.module._reindex_code_index(hnsw.project, hnsw.data, keep_backup=False)

    assert (dirs, items) == (1, 2)
    assert probe_client.collections["code_index_documents"].metadata == {"hnsw:space": "l2"}


def test_without_a_code_index_root_nothing_is_rebuilt(hnsw, capsys):
    assert hnsw.module._reindex_code_index(hnsw.project, hnsw.data, keep_backup=False) == (0, 0)
    assert "no dirs at" in capsys.readouterr().out


def test_a_root_without_commit_dirs_rebuilds_nothing(hnsw, capsys):
    hnsw.commit_dir("PROBE-only").mkdir(parents=True)

    assert hnsw.module._reindex_code_index(hnsw.project, hnsw.data, keep_backup=False) == (0, 0)
    assert "no commit dirs in" in capsys.readouterr().out


def test_keep_backup_copies_the_dir_first_replacing_an_older_backup(hnsw):
    path, _ = _seed_commit(hnsw, "aaa", 1, 1)
    bak = path.with_suffix(".bak")
    bak.mkdir()
    (bak / "stale").write_text("old backup")

    hnsw.module._reindex_code_index(hnsw.project, hnsw.data, keep_backup=True)

    assert (bak / "chroma.sqlite3").read_text() == "db"
    assert not (bak / "stale").exists()


def test_without_keep_backup_no_backup_is_made(hnsw):
    path, _ = _seed_commit(hnsw, "aaa", 1, 1)

    hnsw.module._reindex_code_index(hnsw.project, hnsw.data, keep_backup=False)

    assert not path.with_suffix(".bak").exists()


# -- _reindex_knowledge ------------------------------------------------------


def test_the_knowledge_dir_is_rebuilt_with_its_own_two_collections(hnsw):
    path = hnsw.make_dir(hnsw.knowledge_dir)
    client = hnsw.client(path)
    client.seed("knowledge_documents", 2)
    client.seed("knowledge_chunks", 5)

    assert hnsw.module._reindex_knowledge(hnsw.project, hnsw.data, keep_backup=False) == (1, 7)
    assert client.collections["knowledge_chunks"].metadata == _RUNTIME_METADATA


def test_a_missing_knowledge_dir_rebuilds_nothing(hnsw, capsys):
    assert hnsw.module._reindex_knowledge(hnsw.project, hnsw.data, keep_backup=False) == (0, 0)
    assert "[knowledge] no dir at" in capsys.readouterr().out


def test_knowledge_backup_replaces_an_older_one(hnsw):
    path = hnsw.make_dir(hnsw.knowledge_dir)
    hnsw.client(path).seed("knowledge_documents", 1)
    bak = path.with_suffix(".bak")
    bak.mkdir()
    (bak / "stale").write_text("x")

    hnsw.module._reindex_knowledge(hnsw.project, hnsw.data, keep_backup=True)

    assert (bak / "chroma.sqlite3").exists()
    assert not (bak / "stale").exists()


# -- main --------------------------------------------------------------------


def _both(hnsw):
    _seed_commit(hnsw, "aaa", 2, 2)
    path = hnsw.make_dir(hnsw.knowledge_dir)
    hnsw.client(path).seed("knowledge_documents", 1)
    return path


def test_a_project_that_does_not_exist_exits_2(hnsw, capsys):
    code = hnsw.run(str(hnsw.project / "nope"), "--data-dir", str(hnsw.data))

    assert code == 2
    assert "Project not found" in capsys.readouterr().err


def test_a_full_run_exits_0_with_the_item_and_dir_totals(hnsw, capsys):
    _both(hnsw)

    code = hnsw.run(str(hnsw.project), "--data-dir", str(hnsw.data))

    assert code == 0
    assert "Reindex complete: 5 items across 2 dir(s)" in capsys.readouterr().out


def test_nothing_to_rebuild_exits_1(hnsw, capsys):
    code = hnsw.run(str(hnsw.project), "--data-dir", str(hnsw.data))

    assert code == 1
    assert "Nothing to rebuild." in capsys.readouterr().err


def test_a_failure_exits_1_with_failed_on_stderr(hnsw, capsys):
    _seed_commit(hnsw, "aaa", 3, 1)
    hnsw.client(hnsw.commit_dir("aaa")).lose_rows_in = "code_index_documents__rehnsw_tmp"

    code = hnsw.run(str(hnsw.project), "--data-dir", str(hnsw.data))

    assert code == 1
    assert "FAILED: copy verify failed for code_index_documents" in capsys.readouterr().err


@pytest.mark.parametrize(
    "scope, rebuilt_code, rebuilt_knowledge",
    [("all", True, True), ("code_index", True, False), ("knowledge", False, True)],
)
def test_scope_selects_which_index_is_rebuilt(hnsw, scope, rebuilt_code, rebuilt_knowledge):
    knowledge_path = _both(hnsw)
    code_client = hnsw.client(hnsw.commit_dir("aaa"))
    knowledge_client = hnsw.client(knowledge_path)

    hnsw.run(str(hnsw.project), "--data-dir", str(hnsw.data), "--scope", scope)

    assert (code_client.collections["code_index_documents"].metadata == _RUNTIME_METADATA) is rebuilt_code
    assert (knowledge_client.collections["knowledge_documents"].metadata == _RUNTIME_METADATA) is rebuilt_knowledge


def test_data_dir_defaults_to_the_igni_dir_in_the_home_and_expands_a_tilde(hnsw, tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    path = home / ".igni/projects/proj-id/knowledge.chroma"
    hnsw.make_dir(path)
    hnsw.client(path).seed("knowledge_documents", 1)

    code = hnsw.run(str(hnsw.project), "--scope", "knowledge")

    assert code == 0
    assert hnsw.client(path).collections["knowledge_documents"].metadata == _RUNTIME_METADATA

    other = home / "elsewhere/projects/proj-id/knowledge.chroma"
    hnsw.make_dir(other)
    hnsw.client(other).seed("knowledge_documents", 1)
    assert hnsw.run(str(hnsw.project), "--scope", "knowledge", "--data-dir", "~/elsewhere") == 0
    assert hnsw.client(other).collections["knowledge_documents"].metadata == _RUNTIME_METADATA


def test_the_help_text_names_the_igni_data_dir(hnsw, capsys):
    with pytest.raises(SystemExit):
        hnsw.run("--help")

    out = " ".join(capsys.readouterr().out.split())
    assert "Igni data dir (default: ~/.igni)" in out
