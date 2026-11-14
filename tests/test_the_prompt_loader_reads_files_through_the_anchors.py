"""igni's prompt files, read through the injected table of anchors.

The loader reads ``<directory>/<name>.md``, strips it and fills the
anchors the table knows. Files are UTF-8 whatever the machine's locale.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from igni_core.core import prompts
from igni_core.core.prompts import PROMPTS_DIR, PromptFragments, PromptLoader


def test_a_claimed_anchor_in_a_file_is_filled_and_an_unclaimed_one_is_left(tmp_path):
    (tmp_path / "p.md").write_text("\n\nA {{MINE}} B {{THEIRS}}\n\n", encoding="utf-8")
    fragments = PromptFragments()
    fragments.register("MINE", "acme.ext", lambda: "filled")
    assert PromptLoader(fragments, tmp_path).load("p") == "A filled B {{THEIRS}}"


def test_a_custom_directory_is_read_instead_of_igni_s_own(tmp_path):
    (tmp_path / "main_agent.md").write_text("not igni's", encoding="utf-8")
    loader = PromptLoader(PromptFragments(), tmp_path)
    assert loader.load("main_agent") == "not igni's"
    assert PromptLoader(PromptFragments()).load("main_agent") != "not igni's"


def test_a_missing_file_in_a_custom_directory_is_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        PromptLoader(PromptFragments(), tmp_path).load("nope")


def test_an_empty_file_is_an_empty_prompt(tmp_path):
    (tmp_path / "e.md").write_text("  \n", encoding="utf-8")
    assert PromptLoader(PromptFragments(), tmp_path).load("e") == ""


def test_the_loader_expands_through_the_table_it_was_given():
    fragments = PromptFragments()
    fragments.register("X", "o", lambda: "yes")
    loader = PromptLoader(fragments)
    assert loader.fragments is fragments
    assert loader.expand("{{X}} {{Y}}") == "yes {{Y}}"


def test_the_default_directory_is_the_one_igni_ships_its_prompts_in():
    assert (PROMPTS_DIR / "main_agent.md").is_file()


def test_the_package_exports_what_the_backend_wires():
    assert set(prompts.__all__) == {
        "ANCHOR",
        "PROMPTS_DIR",
        "AnchorRegistration",
        "FragmentSupplier",
        "PromptFragments",
        "PromptLoader",
    }
    assert prompts.PromptLoader is PromptLoader
    assert prompts.PromptFragments is PromptFragments


def test_a_prompt_with_non_ascii_text_reads_on_a_machine_whose_locale_is_not_utf8(tmp_path):
    (tmp_path / "u.md").write_text("naïve — “quoted” ✓", encoding="utf-8")
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from igni_core.core.prompts import PromptFragments, PromptLoader\n"
        "text = PromptLoader(PromptFragments(), Path(sys.argv[1])).load('u')\n"
        "sys.stdout.buffer.write(text.encode('utf-8'))\n"
    )
    env = {
        **os.environ,
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONUTF8": "0",
        "PYTHONCOERCECLOCALE": "0",
        "PYTHONIOENCODING": "utf-8",
    }
    done = subprocess.run(
        [sys.executable, "-c", code, str(Path(tmp_path))],
        env=env,
        capture_output=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr.decode()
    assert done.stdout.decode("utf-8") == "naïve — “quoted” ✓"
