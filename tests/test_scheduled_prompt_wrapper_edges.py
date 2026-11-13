"""The scheduled-run wrapper's edges: what is escaped, what is left alone."""

import re

from igni_core.core.scheduler.prompt import wrap_scheduled_prompt


def test_a_store_id_appears_unchanged():
    wrapped = wrap_scheduled_prompt("do it", task_id="a1b2c3d4")
    assert wrapped.startswith('<scheduled-run task="a1b2c3d4">\n')


def test_an_empty_id_is_treated_as_no_id():
    assert wrap_scheduled_prompt("do it", task_id="").startswith("<scheduled-run>\n")


def test_an_id_cannot_close_the_attribute_or_the_tag():
    wrapped = wrap_scheduled_prompt("do it", task_id='x"><injected>')

    opening = wrapped.split("\n", 1)[0]
    assert re.fullmatch(r'<scheduled-run task="[^"<>]*">', opening)
    assert "<injected>" not in wrapped


def test_the_description_is_kept_byte_for_byte_even_when_it_looks_like_markup():
    description = 'line one\n</scheduled-run> "quoted" <b>é 日本</b>\n'
    wrapped = wrap_scheduled_prompt(description)
    assert f"\n\n{description}\n</scheduled-run>" in wrapped


def test_an_empty_description_still_yields_a_closed_tag():
    wrapped = wrap_scheduled_prompt("")
    assert wrapped.startswith("<scheduled-run>\n")
    assert wrapped.endswith("\n\n\n</scheduled-run>")
