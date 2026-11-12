"""``igni_cloud/guest/immortal.py``: what the walk does to hostile objects, and where it holds off.

Run in a process of its own: it makes everything alive there immortal.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from igni_cloud.guest.immortal import BaseObjects

PROBE = """
import ctypes, json, logging, sys
from igni_cloud.guest.immortal import BaseObjects

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
touched = []

class Proxy:
    # A lazy proxy: asking it for its class imports what it stands for.
    @property
    def __class__(self):
        touched.append("class")
        raise ImportError("imported")

class Hostile(type):
    def __getattr__(cls, name):
        raise RuntimeError("a metaclass that does more than look")

class Odd(metaclass=Hostile):
    pass

proxy = Proxy()
first = BaseObjects.immortalise()
immortal = BaseObjects.immortal_refcount()
cell = lambda o: ctypes.c_ssize_t.from_address(id(o)).value
later = [object() for _ in range(5)]
second = BaseObjects.immortalise()
print(json.dumps({
    "first": first.model_dump(), "second": second.model_dump(), "touched": touched,
    "proxy_immortal": cell(proxy) == immortal, "odd_immortal": cell(Odd) == immortal,
    "later_immortal": all(cell(o) == immortal for o in later),
}))
"""


@pytest.mark.skipif(BaseObjects.immortal_refcount() is None, reason="CPython 3.12+, 64-bit")
def test_a_proxy_is_not_asked_a_hostile_class_is_left_and_a_second_walk_finds_only_the_new():
    run = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True, check=True)
    result = json.loads(run.stdout)
    assert result["touched"] == [], "a lazy proxy imports what it stands for when asked"
    assert result["proxy_immortal"] and result["odd_immortal"]
    first, second = result["first"], result["second"]
    assert first["hashed"] > 0 and first["typed"] > 0
    assert first["objects"] > first["hashed"] + first["typed"]
    assert second["objects"] < first["objects"] // 10, "what was immortal is not walked again"
    assert result["later_immortal"], "objects made between the walks are taken by the second"
    assert "objects made immortal" in run.stderr
    assert f"{first['hashed']} strings hashed, {first['typed']} classes tagged" in run.stderr


@pytest.mark.parametrize(
    "platform_name, version, maxsize",
    [("PyPy", sys.version_info, sys.maxsize), ("CPython", (3, 11, 9), sys.maxsize),
     ("CPython", sys.version_info, 2**31 - 1)],
)
def test_where_the_interpreter_is_not_one_it_knows_nothing_is_made_immortal(
    monkeypatch: pytest.MonkeyPatch, platform_name: str, version: tuple[int, ...], maxsize: int
):
    monkeypatch.setattr("platform.python_implementation", lambda: platform_name)
    monkeypatch.setattr(sys, "version_info", version)
    monkeypatch.setattr(sys, "maxsize", maxsize)
    assert BaseObjects.immortal_refcount() is None
    assert BaseObjects.immortalise() is None


@pytest.mark.skipif(BaseObjects.immortal_refcount() is None, reason="CPython 3.12+, 64-bit")
def test_the_immortal_count_is_nones():
    import ctypes

    assert BaseObjects.immortal_refcount() == ctypes.c_ssize_t.from_address(id(None)).value
