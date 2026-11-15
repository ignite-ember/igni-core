"""What CONFIGURATION.md says igni writes on first run is what it writes.

The page said ``~/.igni/config.yaml`` is "generated from defaults on first
run via ``yaml.dump(DEFAULT_CONFIG)``". No ``DEFAULT_CONFIG`` exists; the
file is a template of comments with no active key (campaign R059). Someone
looking there for the defaults finds none.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from igni_core.core.init_templates import _HOME_CONFIG_BOOTSTRAP, CONFIG_YAML_HEADER

_PAGE = Path(__file__).resolve().parent.parent / "docs/CONFIGURATION.md"


def test_the_first_run_file_sets_nothing():
    assert yaml.safe_load(CONFIG_YAML_HEADER + _HOME_CONFIG_BOOTSTRAP) is None


def test_the_page_does_not_promise_a_dump_of_the_defaults():
    text = _PAGE.read_text()
    assert "DEFAULT_CONFIG" not in text
    assert "comments only" in text
