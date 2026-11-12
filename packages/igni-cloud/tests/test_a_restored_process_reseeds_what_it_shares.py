"""``igni_cloud/guest/entropy.py``: which generators are reseeded, and when."""

from __future__ import annotations

import asyncio
import random
import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest
from igni_cloud.guest.entropy import ProcessEntropy


class Counting(random.Random):
    """A generator that counts how often it was seeded."""

    def __init__(self) -> None:
        self.seeded = 0
        super().__init__(1)

    def seed(self, *args: Any, **kwargs: Any) -> None:
        self.seeded = getattr(self, "seeded", 0) + 1
        super().seed(*args, **kwargs)


class CountingSystem(random.SystemRandom):
    def __init__(self) -> None:
        self.seeded = 0
        super().__init__()

    def seed(self, *args: Any, **kwargs: Any) -> None:
        self.seeded = getattr(self, "seeded", 0) + 1


def _module(monkeypatch: pytest.MonkeyPatch, name: str, **held: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(held)
    monkeypatch.setitem(sys.modules, name, module)
    return module


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(ProcessEntropy, "RESCAN_AFTER_SECONDS", 0.01)
    yield


def test_it_reseeds_what_modules_hold_and_counts_the_random_module_too(
    monkeypatch: pytest.MonkeyPatch,
):
    mine = Counting()
    _module(monkeypatch, "igni_test_entropy_a", rng=mine)
    found = ProcessEntropy._module_generators()
    assert mine in found
    assert ProcessEntropy().reseed() == 1 + len(found)
    assert mine.seeded == 2, "made once, reseeded once"


def test_the_kernels_generator_and_randoms_own_instance_are_left_alone(
    monkeypatch: pytest.MonkeyPatch,
):
    system = CountingSystem()
    _module(monkeypatch, "igni_test_entropy_b", system=system, shared=random._inst)  # type: ignore[attr-defined]
    found = ProcessEntropy._module_generators()
    assert system not in found
    assert random._inst not in found  # type: ignore[attr-defined]
    ProcessEntropy().reseed()
    assert system.seeded == 1, "its constructor's call only: it reads the kernel each time"


def test_a_generator_held_by_several_modules_is_reseeded_once(monkeypatch: pytest.MonkeyPatch):
    mine = Counting()
    _module(monkeypatch, "igni_test_entropy_c1", rng=mine)
    _module(monkeypatch, "igni_test_entropy_c2", also=mine, again=mine)
    assert ProcessEntropy._module_generators().count(mine) == 1
    ProcessEntropy().reseed()
    assert mine.seeded == 2


def test_a_lazy_module_and_a_proxy_are_never_asked_anything(monkeypatch: pytest.MonkeyPatch):
    touched: list[str] = []

    class LazyModule(types.ModuleType):
        @property
        def __dict__(self) -> dict[str, Any]:  # type: ignore[override]
            touched.append("namespace")
            return {}

    class Proxy:
        @property
        def __class__(self) -> type:  # type: ignore[override]
            touched.append("class")
            raise ImportError("the proxy imports what it stands for")

    hidden = Counting()
    lazy = LazyModule("igni_test_entropy_lazy")
    monkeypatch.setitem(sys.modules, lazy.__name__, lazy)
    _module(monkeypatch, "igni_test_entropy_d", proxy=Proxy(), rng=hidden)
    assert hidden in ProcessEntropy._module_generators()
    assert touched == []


def test_what_was_found_is_reseeded_again_without_looking(monkeypatch: pytest.MonkeyPatch):
    mine = Counting()
    _module(monkeypatch, "igni_test_entropy_e", rng=mine)
    entropy = ProcessEntropy()
    assert entropy.reseed_known() == 1, "before any look: the random module only"
    assert mine.seeded == 1
    entropy.reseed()
    late = Counting()
    _module(monkeypatch, "igni_test_entropy_e2", rng=late)
    assert entropy.reseed_known() == 1 + len(entropy._known)
    assert mine.seeded == 3
    assert late.seeded == 1, "made after the look: not known"


async def test_a_later_look_reseeds_only_what_was_made_since(
    monkeypatch: pytest.MonkeyPatch, quick: None
):
    known = Counting()
    _module(monkeypatch, "igni_test_entropy_f", rng=known)
    entropy = ProcessEntropy()
    entropy.reseed()
    new = Counting()
    _module(monkeypatch, "igni_test_entropy_f2", rng=new)
    await entropy.after_restore(None)
    assert (known.seeded, new.seeded) == (3, 1), "the known at once, the new not yet"
    await asyncio.sleep(0.1)
    assert (known.seeded, new.seeded) == (3, 2), "the look reseeds the new only"
    await entropy.after_restore(None)
    assert (known.seeded, new.seeded) == (4, 3), "and what it found is known from now on"
