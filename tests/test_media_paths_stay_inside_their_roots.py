"""Which files a message may put on the wire, and where bare names are found.

A path written out in full is attachable only when it resolves, symlinks
and ``..`` followed, inside the project's uploads directory or the project.
Real files in a temp directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.core.paths import CONFIG_DIR
from igni_core.core.utils.media import MediaResolver
from igni_core.core.utils.media_schemas import MediaKind, MediaTaxonomy


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path.resolve()


@pytest.fixture
def project(root: Path) -> Path:
    path = root / "project"
    path.mkdir()
    return path


@pytest.fixture
def outside(root: Path) -> Path:
    path = root / "outside.png"
    path.write_bytes(b"x")
    return path


def _resolver(project: Path | None, **kwargs) -> MediaResolver:
    return MediaResolver(project_dir=project, search_dirs=(), **kwargs)


def test_a_symlink_in_the_project_to_a_file_outside_it_is_not_attachable(
    project: Path, outside: Path
) -> None:
    link = project / "link.png"
    link.symlink_to(outside)

    resolution = _resolver(project).resolve_text_references(f"see @{link}")

    assert resolution.attachable == []
    assert resolution.paths == [str(outside)]


def test_a_dot_dot_that_leaves_the_project_is_not_attachable(project: Path, outside: Path) -> None:
    resolution = _resolver(project).resolve_text_references(f"see {project}/../outside.png")

    assert resolution.attachable == []
    assert str(outside) in resolution.text


def test_a_file_in_the_uploads_directory_is_attachable(project: Path) -> None:
    uploads = project / CONFIG_DIR / "attachments" / "session-1"
    uploads.mkdir(parents=True)
    upload = uploads / "shot.png"
    upload.write_bytes(b"x")

    resolution = _resolver(project).resolve_text_references(f"look at @{upload}")

    assert resolution.attachable == [str(upload)]
    assert resolution.text == f"look at @{upload}"


def test_a_file_in_the_project_named_in_full_is_attachable(project: Path) -> None:
    image = project / "shot.png"
    image.write_bytes(b"x")

    assert _resolver(project).resolve_text_references(f"{image}").attachable == [str(image)]


def test_two_at_paths_side_by_side_are_both_found(project: Path) -> None:
    first = project / "a.png"
    second = project / "b.png"
    first.write_bytes(b"x")
    second.write_bytes(b"x")

    resolution = _resolver(project).resolve_text_references(f"@{first} @{second}")

    assert resolution.attachable == [str(first), str(second)]


def test_without_a_project_a_path_in_full_stays_text(outside: Path) -> None:
    resolution = _resolver(None).resolve_text_references(f"see {outside}")

    assert resolution.attachable == []
    assert resolution.text == f"see {outside}"


def test_custom_allowed_roots_replace_the_defaults(project: Path, outside: Path) -> None:
    resolution = _resolver(project, allowed_roots=(outside.parent,)).resolve_text_references(
        f"see {outside}"
    )

    assert resolution.attachable == [str(outside)]


def test_the_same_file_named_twice_is_attachable_once(project: Path) -> None:
    image = project / "a.png"
    image.write_bytes(b"x")

    resolution = _resolver(project).resolve_text_references(f"{image} and again {image}")

    assert resolution.attachable == [str(image)]


def test_an_uppercase_extension_is_found(project: Path) -> None:
    image = project / "SHOT.PNG"
    image.write_bytes(b"x")

    resolution = _resolver(project).resolve_text_references("see SHOT.PNG")

    assert resolution.attachable == [str(image)]


def test_a_bare_name_is_looked_for_in_the_project_before_the_search_directories(
    root: Path, project: Path
) -> None:
    downloads = root / "downloads"
    downloads.mkdir()
    (downloads / "a.png").write_bytes(b"x")
    (project / "a.png").write_bytes(b"x")

    resolution = MediaResolver(
        project_dir=project, search_dirs=(downloads,)
    ).resolve_text_references("see a.png")

    assert resolution.attachable == [str(project / "a.png")]


def test_a_bare_name_falls_back_to_the_search_directories_in_order(
    root: Path, project: Path
) -> None:
    first = root / "first"
    second = root / "second"
    first.mkdir()
    second.mkdir()
    (second / "a.png").write_bytes(b"x")
    (first / "b.png").write_bytes(b"x")
    (second / "b.png").write_bytes(b"x")

    resolution = MediaResolver(
        project_dir=project, search_dirs=(first, second)
    ).resolve_text_references("a.png b.png")

    assert resolution.attachable == [str(second / "a.png"), str(first / "b.png")]


def test_names_that_are_found_nowhere_are_reported_in_order(project: Path) -> None:
    (project / "here.png").write_bytes(b"x")

    resolution = _resolver(project).resolve_text_references("gone.png here.png missing.pdf")

    assert resolution.missed == ["gone.png", "missing.pdf"]
    assert resolution.attachable == [str(project / "here.png")]


def test_a_narrower_taxonomy_decides_what_is_media(project: Path) -> None:
    (project / "a.png").write_bytes(b"x")
    (project / "b.pdf").write_bytes(b"x")
    taxonomy = MediaTaxonomy(
        specs=tuple(s for s in MediaTaxonomy.default().specs if s.kind is MediaKind.document)
    )
    resolver = MediaResolver(project_dir=project, taxonomy=taxonomy, search_dirs=())

    resolution = resolver.resolve_text_references("a.png b.pdf")

    assert resolution.attachable == [str(project / "b.pdf")]
    assert resolver.extract_url_media("https://x.test/a.png") is None
    pdf = resolver.extract_url_media("https://x.test/b.pdf")
    assert pdf is not None
    assert pdf.total() == 1
