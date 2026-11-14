"""``NoModelConfigured`` stands in for a model and refuses every way of being called.

It lets a session build with nothing usable configured, so the person can
reach ``/login``. Each of the four ways a model is invoked (sync and async,
whole and streamed) raises the same ``ValueError`` carrying the remedy; the
streamed ones raise when iterated, which is when the agent first touches
them. Two placeholders never share a message.
"""

from __future__ import annotations

import pytest

from igni_core.core.config.null_model import NoModelConfigured
from igni_core.core.paths import DEFAULT_DATA_DIR


def test_it_is_an_openai_like_model_with_a_recognisable_id():
    from agno.models.openai.like import OpenAILike

    model = NoModelConfigured.for_login_required()

    assert isinstance(model, OpenAILike)
    assert model.id == "(no model configured)"


def test_the_login_message_points_at_the_config_file_and_login_not_a_service():
    message = NoModelConfigured.for_login_required().ERROR_MESSAGE

    assert message.startswith("No model configured.")
    assert f"{DEFAULT_DATA_DIR}/config.yaml" in message
    assert "/login" in message
    assert "Cloud" not in message
    assert "{DEFAULT_DATA_DIR}" not in message


def test_the_stale_message_names_the_model_and_the_real_config_path():
    message = NoModelConfigured.for_stale_default("gone-model").ERROR_MESSAGE

    assert "'gone-model'" in message
    assert "/login" in message
    assert "/model" in message
    assert f"{DEFAULT_DATA_DIR}/config.yaml" in message
    assert "{" not in message


@pytest.mark.parametrize("name", ["{name}", "a{b}c", "{0}", "modèle-ü", "}{"])
def test_a_model_name_with_braces_or_unicode_is_shown_as_written(name):
    message = NoModelConfigured.for_stale_default(name).ERROR_MESSAGE

    assert repr(name) in message


def test_a_stale_placeholder_does_not_change_anyone_elses_message():
    stale = NoModelConfigured.for_stale_default("gone-model")
    fresh = NoModelConfigured.for_login_required()

    assert "gone-model" not in fresh.ERROR_MESSAGE
    assert "gone-model" not in NoModelConfigured.ERROR_MESSAGE
    assert "gone-model" in stale.ERROR_MESSAGE


def test_invoke_raises_the_message():
    model = NoModelConfigured.for_stale_default("gone-model")

    with pytest.raises(ValueError) as caught:
        model.invoke([])

    assert str(caught.value) == model.ERROR_MESSAGE


def test_invoke_stream_raises_when_iterated():
    model = NoModelConfigured.for_stale_default("gone-model")

    with pytest.raises(ValueError) as caught:
        list(model.invoke_stream([]))

    assert str(caught.value) == model.ERROR_MESSAGE


async def test_ainvoke_raises_the_message():
    model = NoModelConfigured.for_stale_default("gone-model")

    with pytest.raises(ValueError) as caught:
        await model.ainvoke([])

    assert str(caught.value) == model.ERROR_MESSAGE


async def test_ainvoke_stream_raises_when_iterated():
    model = NoModelConfigured.for_stale_default("gone-model")

    with pytest.raises(ValueError) as caught:
        async for _ in model.ainvoke_stream([]):
            pass

    assert str(caught.value) == model.ERROR_MESSAGE
