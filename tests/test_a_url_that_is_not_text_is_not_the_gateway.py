"""``is_cloud_gateway_url`` answers False for what is not a url, never raises.

The migration asks it about every stored row's ``url``, which is whatever
a person's YAML held: a number or nothing at all is not the gateway.
"""

from __future__ import annotations

import pytest

from igni_core.core.config.model_entry import ModelRegistryEntry


@pytest.mark.parametrize("value", [None, 12, 1.5, b"https://api.ignite-ember.sh", ["x"]])
def test_a_value_that_is_not_text_is_not_the_gateway(value):
    assert ModelRegistryEntry.is_cloud_gateway_url(value) is False  # type: ignore[arg-type]
