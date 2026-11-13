"""An extension asset's declaration, download, cache and unpacking, at their edges.

What a manifest declares is untrusted, and so are the bytes behind it:
a bad declaration is refused when the manifest is read, a bad download is
never kept, and a bad archive never leaves anything behind or outside.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import re
import stat
import tarfile
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from igni_core.extensions import assets as assets_module
from igni_core.extensions.assets import (
    MAX_ASSET_BYTES,
    AssetCache,
    AssetError,
    AssetLayout,
    AssetPlatform,
    AssetSource,
    ExtensionAsset,
    ExtensionAssets,
    FileDigest,
    ReadOnlyTree,
    Unpacker,
)
from tests.extension_distribution_helpers import asset, tar_gz


def declared(body: bytes = b"bytes", **changes: Any) -> ExtensionAsset:
    return ExtensionAsset.model_validate({**asset("neo4j", "any", body), **changes})


def zip_of(members: dict[str, bytes], *, mode: int | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        for name, data in members.items():
            info = zipfile.ZipInfo(name)
            if mode is not None:
                info.external_attr = mode << 16
            zipped.writestr(info, data)
    return buffer.getvalue()


def test_the_process_is_told_where_its_assets_are_by_an_igni_variable():
    assert assets_module.ASSETS_DIR_ENV == "IGNI_EXTENSION_ASSETS"
    assert assets_module.BUNDLE_ASSETS == "assets"
    source = Path(assets_module.__file__).read_text(encoding="utf-8")
    assert not re.search(r"\bember\b", source, re.IGNORECASE)


# ── The declaration ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "Neo4j"},
        {"name": "-neo4j"},
        {"name": "n" * 65},
        {"name": ""},
        {"name": "neo_4j"},
        {"file": "dir/file.bin"},
        {"file": "dir\\file.bin"},
        {"file": ".."},
        {"file": ".hidden"},
        {"file": "f" * 201},
        {"file": "fichieré.bin"},
        {"file": ""},
        {"size": 0},
        {"size": -1},
        {"size": MAX_ASSET_BYTES + 1},
        {"sha256": "AB" * 32},
        {"sha256": "ab" * 31},
        {"sha256": "ab" * 33},
        {"source": "http://downloads.example/x"},
        {"source": "https://down loads.example/x"},
        {"source": "https://h/" + "x" * 2000},
        {"platform": "freebsd"},
        {"unpack": True},
        {"surprise": 1},
    ],
    ids=lambda c: next(iter(c)) + "=" + str(next(iter(c.values())))[:12],
)
def test_a_declaration_outside_the_rules_is_refused(changes):
    with pytest.raises(ValidationError):
        declared(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "a"},
        {"name": "n" * 64},
        {"name": "neo4j-5"},
        {"file": "neo4j-5.26.0+x_y.tar.gz", "unpack": True},
        {"file": "a" + "f" * 199},
        {"size": MAX_ASSET_BYTES},
        {"file": "a.tgz", "unpack": True},
        {"file": "a.zip", "unpack": True},
    ],
)
def test_a_declaration_at_the_rules_edge_is_taken(changes):
    declared(**changes)


def test_a_declaration_does_not_change_once_read():
    with pytest.raises(ValidationError):
        declared().name = "other"  # type: ignore[misc]


def test_a_non_archive_may_be_declared_as_it_is():
    assert declared(file="model.onnx", unpack=False).unpack is False


def test_an_asset_set_naming_one_file_twice_for_any_is_refused():
    with pytest.raises(ValueError, match="one file for 'any' platform"):
        ExtensionAssets.check([declared(), declared()])


def test_an_asset_set_mixing_any_and_a_platform_is_refused():
    with pytest.raises(ValueError, match="it is for any, macos-arm64"):
        ExtensionAssets.check([declared(), declared(platform="macos-arm64")])


def test_an_asset_set_for_every_platform_is_taken_in_any_order():
    every = [declared(platform=str(p)) for p in reversed(AssetPlatform.specific())]
    assert ExtensionAssets.check(every) == every


def test_a_platform_named_twice_is_refused():
    platforms = [*AssetPlatform.specific(), AssetPlatform.MACOS_ARM64]
    with pytest.raises(ValueError, match="one for each of"):
        ExtensionAssets.check([declared(platform=str(p)) for p in platforms])


def test_for_platform_gives_the_shared_and_that_platforms_only():
    mac = declared(name="jre", platform="macos-arm64")
    linux = declared(name="jre", platform="linux-x86_64")
    shared = declared(name="model")
    got = ExtensionAssets.for_platform([mac, linux, shared], AssetPlatform.LINUX_X86_64)
    assert got == [linux, shared]


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("darwin", "arm64", AssetPlatform.MACOS_ARM64),
        ("darwin", "aarch64", AssetPlatform.MACOS_ARM64),
        ("linux", "x86_64", AssetPlatform.LINUX_X86_64),
        ("linux2", "AMD64", AssetPlatform.LINUX_X86_64),
        ("win32", "AMD64", AssetPlatform.WINDOWS_X86_64),
        ("win32", "x86_64", AssetPlatform.WINDOWS_X86_64),
        ("darwin", "x86_64", None),
        ("linux", "aarch64", None),
        ("win32", "arm64", None),
        ("freebsd14", "x86_64", None),
    ],
)
def test_a_machine_is_named_by_system_and_architecture(monkeypatch, system, machine, expected):
    monkeypatch.setattr(assets_module.sys, "platform", system)
    monkeypatch.setattr(assets_module.host_platform, "machine", lambda: machine)
    assert AssetPlatform.current() is expected


def test_the_platforms_igni_runs_on_are_the_three_real_ones():
    assert set(AssetPlatform.specific()) == {
        AssetPlatform.MACOS_ARM64,
        AssetPlatform.LINUX_X86_64,
        AssetPlatform.WINDOWS_X86_64,
    }


# ── Digests, sealing, the cache ────────────────────────────────────


def test_a_file_matches_only_its_pinned_size_and_digest(tmp_path):
    body = b"x" * (2 * 1024 * 1024 + 5)  # read in more than one chunk
    pinned = declared(body)
    path = tmp_path / "f"
    path.write_bytes(body)
    assert FileDigest.of(path) == hashlib.sha256(body).hexdigest()
    assert FileDigest.matches(path, pinned)
    path.write_bytes(body[:-1] + b"y")
    assert not FileDigest.matches(path, pinned), "same size, other bytes"
    path.write_bytes(body + b"y")
    assert not FileDigest.matches(path, pinned), "longer"
    assert not FileDigest.matches(tmp_path, pinned), "a directory"
    assert not FileDigest.matches(tmp_path / "missing", pinned)


def test_the_digest_of_an_empty_file_is_the_empty_digest(tmp_path):
    path = tmp_path / "empty"
    path.write_bytes(b"")
    assert FileDigest.of(path) == hashlib.sha256(b"").hexdigest()


def test_sealing_strips_write_bits_everywhere_but_keeps_exec_and_skips_links(tmp_path):
    root = tmp_path / "tree"
    (root / "a" / "b").mkdir(parents=True)
    run = root / "a" / "run.sh"
    run.write_text("#!/bin/sh\n")
    run.chmod(0o775)
    (root / "a" / "b" / "data").write_text("d")
    (root / "dangling").symlink_to(tmp_path / "nowhere")
    outside = tmp_path / "outside"
    outside.write_text("o")
    outside.chmod(0o644)
    (root / "link").symlink_to(outside)

    ReadOnlyTree.seal(root)

    for path in (root, root / "a", root / "a" / "b", run, root / "a" / "b" / "data"):
        assert not stat.S_IMODE(path.stat().st_mode) & 0o222, path
    assert stat.S_IMODE(run.stat().st_mode) == 0o555
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644, "a link's target is not touched"
    ReadOnlyTree.remove(root)
    assert not root.exists()


def test_removing_what_is_not_there_is_silent(tmp_path):
    ReadOnlyTree.remove(tmp_path / "missing")


def test_removing_a_dangling_link_removes_the_link(tmp_path):
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "nowhere")
    ReadOnlyTree.remove(link)
    assert not link.is_symlink()


def test_a_partial_download_is_a_fresh_file_in_a_cache_that_is_made_for_it(tmp_path):
    cache = AssetCache(tmp_path / "deep" / "cache")
    first, second = cache.partial(), cache.partial()
    assert first != second and first.parent == cache.root and first.is_file()


def test_the_cache_holds_nothing_when_its_directory_is_not_there(tmp_path):
    assert not AssetCache(tmp_path / "none").holds(declared())


def test_the_cache_does_not_hold_a_file_with_the_digests_name_but_other_bytes(tmp_path):
    cache = AssetCache(tmp_path)
    pinned = declared(b"right")
    cache.path(pinned).write_bytes(b"wrong")
    assert not cache.holds(pinned)
    cache.path(pinned).write_bytes(b"right")
    assert cache.holds(pinned)


def test_a_partial_that_is_not_the_pinned_bytes_is_refused_and_removed(tmp_path):
    cache = AssetCache(tmp_path)
    partial = cache.partial()
    partial.write_bytes(b"other")
    with pytest.raises(AssetError, match="its manifest pins"):
        cache.adopt(partial, declared(b"right"))
    assert not partial.exists()
    assert not cache.path(declared(b"right")).exists()


def test_a_partial_that_is_the_pinned_bytes_is_kept_under_its_digest(tmp_path):
    cache = AssetCache(tmp_path)
    partial = cache.partial()
    partial.write_bytes(b"right")
    kept = cache.adopt(partial, declared(b"right"))
    assert kept == cache.path(declared(b"right")) and kept.read_bytes() == b"right"
    assert not partial.exists()


def test_pruning_keeps_what_is_named_and_forgets_the_rest(tmp_path):
    cache = AssetCache(tmp_path)
    (tmp_path / "keep").write_text("k")
    (tmp_path / "drop").write_text("d")
    cache.prune({"keep"})
    assert [p.name for p in tmp_path.iterdir()] == ["keep"]


def test_pruning_a_cache_that_is_not_there_is_silent(tmp_path):
    AssetCache(tmp_path / "none").prune(set())


# ── The download ───────────────────────────────────────────────────


def fetch(tmp_path: Path, handler, body: bytes = b"right") -> tuple[Path, AssetCache]:
    cache = AssetCache(tmp_path / "cache")
    source = AssetSource(transport=httpx.MockTransport(handler))
    return source.fetch(declared(body), cache), cache


def leftovers(cache: AssetCache) -> list[str]:
    return sorted(p.name for p in cache.root.iterdir()) if cache.root.is_dir() else []


def test_a_download_of_the_pinned_bytes_is_kept(tmp_path):
    path, cache = fetch(tmp_path, lambda r: httpx.Response(200, content=b"right"))
    assert path.read_bytes() == b"right" and leftovers(cache) == [path.name]


def test_a_redirect_to_https_is_followed(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "downloads.example":
            return httpx.Response(302, headers={"location": "https://cdn.example/x"})
        return httpx.Response(200, content=b"right")

    path, _ = fetch(tmp_path, handler)
    assert path.read_bytes() == b"right"


@pytest.mark.parametrize("status", [404, 500, 204, 206])
def test_an_answer_other_than_200_is_refused_and_leaves_nothing(tmp_path, status):
    cache = AssetCache(tmp_path / "cache")
    source = AssetSource(transport=httpx.MockTransport(lambda r: httpx.Response(status)))
    with pytest.raises(AssetError, match=f"answered {status}"):
        source.fetch(declared(), cache)
    assert leftovers(cache) == []


def test_a_redirect_landing_on_http_is_refused_and_leaves_nothing(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "https":
            return httpx.Response(302, headers={"location": "http://cdn.example/x"})
        return httpx.Response(200, content=b"right")

    cache = AssetCache(tmp_path / "cache")
    with pytest.raises(AssetError, match="answered 200 from http://cdn.example/x"):
        AssetSource(transport=httpx.MockTransport(handler)).fetch(declared(), cache)
    assert leftovers(cache) == []


def test_a_body_larger_than_pinned_is_refused_and_leaves_nothing(tmp_path):
    cache = AssetCache(tmp_path / "cache")
    served = httpx.Response(200, content=b"right and then some")
    with pytest.raises(AssetError, match="larger than the 5 bytes"):
        AssetSource(transport=httpx.MockTransport(lambda r: served)).fetch(declared(), cache)
    assert leftovers(cache) == []


def test_a_body_larger_is_refused_while_it_streams_not_after(tmp_path):
    chunks = 0

    def body():
        nonlocal chunks
        for _ in range(100):
            chunks += 1
            yield b"x" * 600_000

    cache = AssetCache(tmp_path / "cache")
    pinned = declared(b"x" * 1_000_000)
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=body()))
    with pytest.raises(AssetError, match="larger than"):
        AssetSource(transport=transport).fetch(pinned, cache)
    assert chunks < 100


@pytest.mark.parametrize("served", [b"righ", b"wrong", b""])
def test_a_body_that_is_not_the_pinned_bytes_is_refused_and_leaves_nothing(tmp_path, served):
    cache = AssetCache(tmp_path / "cache")
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=served))
    with pytest.raises(AssetError, match="its manifest pins"):
        AssetSource(transport=transport).fetch(declared(), cache)
    assert leftovers(cache) == []


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")], ids=["connect", "timeout"]
)
def test_a_transport_failure_is_an_asset_error_and_leaves_nothing(tmp_path, error):
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    cache = AssetCache(tmp_path / "cache")
    with pytest.raises(AssetError, match="could not be fetched"):
        AssetSource(transport=httpx.MockTransport(handler)).fetch(declared(), cache)
    assert leftovers(cache) == []


# ── Laying out ─────────────────────────────────────────────────────


def lay(tmp_path: Path, pinned: ExtensionAsset, body: bytes) -> tuple[AssetLayout, Path, Path]:
    cache = AssetCache(tmp_path / "cache")
    partial = cache.partial()
    partial.write_bytes(body)
    cache.adopt(partial, pinned)
    bundle = tmp_path / "bundle"
    bundle.mkdir(exist_ok=True)
    return AssetLayout(cache), bundle, bundle / "assets" / pinned.name


def test_a_failure_while_unpacking_leaves_no_staging_and_no_target(tmp_path):
    body = b"not a tar at all"
    pinned = declared(body, file="x.tar.gz", unpack=True)
    layout, bundle, target = lay(tmp_path, pinned, body)
    with pytest.raises(AssetError, match="could not be unpacked"):
        layout.lay_out(bundle, [pinned])
    assert sorted(p.name for p in (bundle / "assets").iterdir()) == []


def test_a_staging_left_by_a_crash_is_cleared_and_the_asset_laid_out(tmp_path):
    body = b"model bytes"
    pinned = declared(body, file="m.onnx")
    layout, bundle, target = lay(tmp_path, pinned, body)
    stale = bundle / "assets" / ".neo4j.incoming"
    (stale / "old").mkdir(parents=True)
    (stale / "old" / "junk").write_text("j")
    ReadOnlyTree.seal(stale)
    layout.lay_out(bundle, [pinned])
    assert (target / "m.onnx").read_bytes() == body
    assert not stale.exists()


def test_a_marker_for_other_bytes_lays_the_asset_out_again(tmp_path):
    body = b"model bytes"
    pinned = declared(body, file="m.onnx")
    layout, bundle, target = lay(tmp_path, pinned, body)
    target.mkdir(parents=True)
    (target / ".sha256").write_text("0" * 64 + "\n")
    (target / "m.onnx").write_bytes(b"stale")
    layout.lay_out(bundle, [pinned])
    assert (target / "m.onnx").read_bytes() == body
    assert (target / ".sha256").read_text().strip() == pinned.sha256


def test_a_marker_that_matches_but_a_cache_that_is_gone_still_leaves_it_alone(tmp_path):
    body = b"model bytes"
    pinned = declared(body, file="m.onnx")
    layout, bundle, target = lay(tmp_path, pinned, body)
    layout.lay_out(bundle, [pinned])
    layout._cache.path(pinned).unlink()
    layout.lay_out(bundle, [pinned])  # nothing to fetch: already laid out from this digest
    assert (target / "m.onnx").read_bytes() == body


def test_an_empty_marker_is_not_a_match(tmp_path):
    body = b"model bytes"
    pinned = declared(body, file="m.onnx")
    layout, bundle, target = lay(tmp_path, pinned, body)
    target.mkdir(parents=True)
    (target / ".sha256").write_text("")
    layout.lay_out(bundle, [pinned])
    assert (target / ".sha256").read_text().strip() == pinned.sha256


def test_a_laid_out_asset_is_sealed_and_marked(tmp_path):
    body = b"model bytes"
    pinned = declared(body, file="m.onnx")
    layout, bundle, target = lay(tmp_path, pinned, body)
    assert layout.lay_out(bundle, [pinned]) == bundle / "assets"
    assert not stat.S_IMODE((target / "m.onnx").stat().st_mode) & 0o222
    assert not stat.S_IMODE(target.stat().st_mode) & 0o222


def test_an_interrupt_while_laying_out_cleans_the_staging_up(tmp_path, monkeypatch):
    body = b"model bytes"
    pinned = declared(body, file="m.onnx")
    layout, bundle, target = lay(tmp_path, pinned, body)

    def interrupted(*args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(assets_module.shutil, "copyfile", interrupted)
    with pytest.raises(KeyboardInterrupt):
        layout.lay_out(bundle, [pinned])
    assert list((bundle / "assets").iterdir()) == []


# ── Unpacking ──────────────────────────────────────────────────────


def unpack(tmp_path: Path, body: bytes, name: str) -> Path:
    archive = tmp_path / name
    archive.write_bytes(body)
    into = tmp_path / "into"
    into.mkdir()
    Unpacker.unpack(archive, into, name)
    return into


def tar_with(info: tarfile.TarInfo, data: bytes = b"") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_a_tgz_unpacks_the_same_as_a_tar_gz(tmp_path):
    into = unpack(tmp_path, tar_gz({"a/b": b"x"}, executable=("a/b",)), "x.tgz")
    assert (into / "a" / "b").read_bytes() == b"x"
    assert os.access(into / "a" / "b", os.X_OK)


def test_a_tar_member_with_an_absolute_path_lands_inside_and_never_at_that_path(tmp_path):
    name = f"/{tmp_path.name}-igni-absolute-escape"
    into = unpack(tmp_path, tar_gz({name: b"x"}), "x.tar.gz")
    assert not Path(name).exists()
    assert (into / name.lstrip("/")).read_bytes() == b"x"


def test_a_tar_link_pointing_outside_is_refused(tmp_path):
    link = tarfile.TarInfo("out")
    link.type = tarfile.SYMTYPE
    link.linkname = "../../../etc/passwd"
    with pytest.raises(AssetError, match="could not be unpacked"):
        unpack(tmp_path, tar_with(link), "x.tar.gz")


def test_a_tar_device_file_is_refused(tmp_path):
    device = tarfile.TarInfo("dev")
    device.type = tarfile.CHRTYPE
    with pytest.raises(AssetError, match="could not be unpacked"):
        unpack(tmp_path, tar_with(device), "x.tar.gz")


@pytest.mark.parametrize("damage", ["truncated", "not gzip", "bad deflate"])
def test_a_damaged_tar_is_an_asset_error(tmp_path, damage):
    good = tar_gz({"a": os.urandom(50_000)})
    body = {
        "truncated": good[: len(good) // 2],
        "not gzip": b"plain text, not an archive",
        "bad deflate": good[:40] + bytes(b ^ 0xFF for b in good[40:80]) + good[80:],
    }[damage]
    with pytest.raises(AssetError, match="could not be unpacked"):
        unpack(tmp_path, body, "x.tar.gz")


def test_a_tar_declaring_more_than_the_limit_unpacked_is_refused_before_writing(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(assets_module, "MAX_UNPACKED_BYTES", 10)
    with pytest.raises(AssetError, match="unpacks to more than igni allows"):
        unpack(tmp_path, tar_gz({"a": b"x" * 6, "b": b"y" * 6}), "x.tar.gz")
    assert list((tmp_path / "into").iterdir()) == []


def test_a_tar_at_the_limit_unpacks(tmp_path, monkeypatch):
    monkeypatch.setattr(assets_module, "MAX_UNPACKED_BYTES", 12)
    into = unpack(tmp_path, tar_gz({"a": b"x" * 6, "b": b"y" * 6}), "x.tar.gz")
    assert (into / "b").read_bytes() == b"y" * 6


def test_a_zip_declaring_more_than_the_limit_unpacked_is_refused_before_writing(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(assets_module, "MAX_UNPACKED_BYTES", 10)
    with pytest.raises(AssetError, match="unpacks to more than igni allows"):
        unpack(tmp_path, zip_of({"a": b"x" * 6, "b": b"y" * 6}), "x.zip")
    assert list((tmp_path / "into").iterdir()) == []


def test_a_zip_with_an_absolute_entry_is_refused(tmp_path):
    with pytest.raises(AssetError, match="outside itself"):
        unpack(tmp_path, zip_of({"/tmp/igni-absolute-escape": b"x"}), "x.zip")


def test_a_zip_link_is_refused_and_nothing_is_written(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        zipped.writestr("ok", b"fine")
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zipped.writestr(info, b"/etc/passwd")
    with pytest.raises(AssetError, match="has a link: link"):
        unpack(tmp_path, buffer.getvalue(), "x.zip")
    assert list((tmp_path / "into").iterdir()) == []


def test_a_zip_keeps_the_executable_bit_and_makes_its_directories(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        zipped.writestr(zipfile.ZipInfo("bin/"), b"")
        info = zipfile.ZipInfo("bin/run")
        info.external_attr = (stat.S_IFREG | 0o755) << 16
        zipped.writestr(info, b"#!/bin/sh\n")
        plain = zipfile.ZipInfo("bin/data")
        plain.external_attr = (stat.S_IFREG | 0o644) << 16
        zipped.writestr(plain, b"d")
    into = unpack(tmp_path, buffer.getvalue(), "x.zip")
    assert stat.S_IMODE((into / "bin" / "run").stat().st_mode) == 0o755
    assert not os.access(into / "bin" / "data", os.X_OK)
    assert (into / "bin").is_dir()


@pytest.mark.parametrize("damage", ["not a zip", "truncated", "bad data"])
def test_a_damaged_zip_is_an_asset_error(tmp_path, damage):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr("a", os.urandom(2000) + b"a" * 50_000)
    good = buffer.getvalue()
    body = {
        "not a zip": b"plain text",
        "truncated": good[: len(good) // 2],
        "bad data": good[:60] + bytes(b ^ 0xFF for b in good[60:90]) + good[90:],
    }[damage]
    with pytest.raises(AssetError, match="could not be unpacked"):
        unpack(tmp_path, body, "x.zip")


def test_gzip_of_a_non_tar_is_an_asset_error(tmp_path):
    with pytest.raises(AssetError, match="could not be unpacked"):
        unpack(tmp_path, gzip.compress(b"not a tar"), "x.tar.gz")
