"""igni-cloud ships at igni-core's version and pins igni-core to exactly that.

igni-cloud is built from this repository beside igni-core, and the guest
image and the host venv install the two wheels side by side. Its
dependency is ``igni-core==<version>``: the layer plugs into the core's
seams, so it is only good with the core it was built with. When the root
was bumped to 1.0.5 and igni-cloud was not, the pair could not be
installed together at all: ``uv pip install igni_core-1.0.5…whl
igni_cloud-1.0.4…whl`` answered "igni-cloud==1.0.4 depends on
igni-core==1.0.4 … your requirements are unsatisfiable", which stops the
guest image build and the Fleet node's host venv.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CORE = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"]
_CLOUD = tomllib.loads((_ROOT / "packages" / "igni-cloud" / "pyproject.toml").read_text())[
    "project"
]


def _core_requirements() -> list[str]:
    return [
        d.replace(" ", "")
        for d in _CLOUD["dependencies"]
        if d.replace(" ", "").startswith("igni-core")
    ]


def test_igni_cloud_is_released_at_the_core_version():
    assert _CLOUD["version"] == _CORE["version"], (
        f"igni-cloud is {_CLOUD['version']} but igni-core is {_CORE['version']}: bump both together"
    )


def test_igni_cloud_pins_the_core_it_is_built_with():
    assert _core_requirements() == [f"igni-core=={_CORE['version']}"], (
        f"igni-cloud requires {_core_requirements()}, but the core beside it is {_CORE['version']}"
    )
