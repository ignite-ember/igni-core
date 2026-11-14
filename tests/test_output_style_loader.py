"""Output-style loader: file parsing, skipping and root ordering."""

from __future__ import annotations

import dataclasses

import pytest

from igni_core.core.output_styles import OutputStyle, discover_output_styles
from igni_core.core.output_styles.loader import _parse_frontmatter
from igni_core.core.paths import CONFIG_DIR


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: h)
    return h


@pytest.fixture
def proj(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    return p


def _styles(proj):
    return discover_output_styles(proj, read_claude=False)


def test_frontmatter_that_is_not_a_mapping_is_no_metadata():
    meta, body = _parse_frontmatter("---\n- a\n- b\n---\nBody\n")
    assert meta == {}
    assert body == "Body\n"


def test_empty_frontmatter_block_is_no_metadata():
    meta, body = _parse_frontmatter("---\n\n---\nBody\n")
    assert meta == {}
    assert body == "Body\n"


def test_later_rule_line_stays_in_body(home, proj):
    _write(
        proj / CONFIG_DIR / "output-styles" / "s.md",
        "---\nname: s\n---\nOne\n---\nTwo\n",
    )
    assert _styles(proj)["s"].body == "One\n---\nTwo"


def test_crlf_file_still_gets_its_metadata(home, proj):
    _write(
        proj / CONFIG_DIR / "output-styles" / "s.md",
        "---\r\nname: crlf\r\ndescription: d\r\n---\r\nBody\r\n",
    )
    style = _styles(proj)["crlf"]
    assert style.description == "d"
    assert style.body == "Body"


def test_fields_are_stripped_and_coerced(home, proj):
    _write(
        proj / CONFIG_DIR / "output-styles" / "f.md",
        "---\nname: '  spaced  '\ndescription: 42\n---\n\n  Body  \n\n",
    )
    style = _styles(proj)["spaced"]
    assert style.description == "42"
    assert style.body == "Body"


@pytest.mark.parametrize("name_line", ["name:", "name: ''", "name: '   '", "name: null", "name: ~"])
def test_missing_or_empty_name_falls_back_to_stem(home, proj, name_line):
    _write(proj / CONFIG_DIR / "output-styles" / "stem.md", f"---\n{name_line}\n---\nB\n")
    assert list(_styles(proj)) == ["stem"]


def test_unicode_name_and_body(home, proj):
    _write(proj / CONFIG_DIR / "output-styles" / "u.md", "---\nname: стиль\n---\nТекст ✓\n")
    style = _styles(proj)["стиль"]
    assert style.body == "Текст ✓"


def test_dotfile_is_skipped(home, proj):
    _write(proj / CONFIG_DIR / "output-styles" / ".hidden.md", "Secret\n")
    _write(proj / CONFIG_DIR / "output-styles" / "shown.md", "Shown\n")
    assert list(_styles(proj)) == ["shown"]


def test_non_utf8_file_and_directory_named_md_are_skipped(home, proj):
    root = proj / CONFIG_DIR / "output-styles"
    root.mkdir(parents=True)
    (root / "bad.md").write_bytes(b"\xff\xfe\x00bad")
    (root / "dir.md").mkdir()
    _write(root / "ok.md", "Fine\n")
    assert list(_styles(proj)) == ["ok"]


def test_missing_roots_and_empty_dir_give_empty_result(home, proj):
    assert discover_output_styles(proj) == {}
    (proj / CONFIG_DIR / "output-styles").mkdir(parents=True)
    assert discover_output_styles(proj, plugin_roots=[], group_dir=proj / "nope") == {}


def test_non_md_files_are_ignored(home, proj):
    _write(proj / CONFIG_DIR / "output-styles" / "notes.txt", "x")
    assert _styles(proj) == {}


def test_path_is_resolved_through_symlink(home, proj, tmp_path):
    real = tmp_path / "real.md"
    real.write_text("Linked\n", encoding="utf-8")
    root = proj / CONFIG_DIR / "output-styles"
    root.mkdir(parents=True)
    (root / "link.md").symlink_to(real)
    assert _styles(proj)["link"].path == real.resolve()


def test_output_style_is_immutable():
    style = OutputStyle(name="a", path=None, description="", body="")  # type: ignore[arg-type]
    with pytest.raises(dataclasses.FrozenInstanceError):
        style.name = "b"  # type: ignore[misc]


def test_same_name_in_one_root_last_filename_wins(home, proj):
    root = proj / CONFIG_DIR / "output-styles"
    _write(root / "a.md", "---\nname: dup\n---\nFROM A\n")
    _write(root / "b.md", "---\nname: dup\n---\nFROM B\n")
    assert _styles(proj)["dup"].body == "FROM B"


def test_frontmatter_name_is_the_key_not_the_stem(home, proj):
    _write(proj / CONFIG_DIR / "output-styles" / "x.md", "---\nname: y\n---\nB\n")
    _write(proj / CONFIG_DIR / "output-styles" / "y.md", "Stem y\n")
    # x.md sorts first, so y.md's stem-named style overwrites x.md's "y".
    assert _styles(proj)["y"].body == "Stem y"


def test_claude_roots_are_read_only_when_asked_and_igni_beats_claude(home, proj):
    _write(home / ".claude" / "output-styles" / "u.md", "USER CLAUDE\n")
    _write(home / CONFIG_DIR / "output-styles" / "u.md", "USER IGNI\n")
    _write(proj / ".claude" / "output-styles" / "p.md", "PROJECT CLAUDE\n")
    _write(proj / CONFIG_DIR / "output-styles" / "p.md", "PROJECT IGNI\n")
    _write(proj / ".claude" / "output-styles" / "only.md", "ONLY CLAUDE\n")

    on = discover_output_styles(proj, read_claude=True)
    assert on["u"].body == "USER IGNI"
    assert on["p"].body == "PROJECT IGNI"
    assert on["only"].body == "ONLY CLAUDE"

    off = discover_output_styles(proj, read_claude=False)
    assert "only" not in off


def test_project_claude_beats_user_igni(home, proj):
    _write(home / CONFIG_DIR / "output-styles" / "s.md", "USER IGNI\n")
    _write(proj / ".claude" / "output-styles" / "s.md", "PROJECT CLAUDE\n")
    assert discover_output_styles(proj, read_claude=True)["s"].body == "PROJECT CLAUDE"


def test_later_plugin_root_beats_earlier(home, proj, tmp_path):
    p1, p2 = tmp_path / "p1", tmp_path / "p2"
    _write(p1 / "output-styles" / "t.md", "ONE\n")
    _write(p2 / "output-styles" / "t.md", "TWO\n")
    out = discover_output_styles(proj, plugin_roots=[(p1, "a"), (p2, "b")], read_claude=False)
    assert out["t"].body == "TWO"
    out = discover_output_styles(proj, plugin_roots=[(p2, "b"), (p1, "a")], read_claude=False)
    assert out["t"].body == "ONE"
