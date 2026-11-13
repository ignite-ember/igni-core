"""The ``<loop-iteration>`` wrapper the model reads on every loop iteration.

Model-facing text, byte for byte: the model is fine-tuned on it.
"""

from __future__ import annotations

from igni_core.core.loop import wrap_iteration_prompt

BODY = (
    "Autonomous loop iteration — do not ask the user; perform one unit of work and stop. "
    "Tool-permission prompts are the only legitimate user interaction.\n"
    "\n"
    "When you can determine the total number of items (e.g. after listing files or parsing "
    "the input), call loop_set_total(N) once so the panel renders progress as N/total. "
    "Call loop_stop() when all work is done — don't keep looping just because the safety "
    "cap hasn't been hit.\n"
    "\n"
)


def test_an_implicit_cap_iteration_is_wrapped_byte_for_byte():
    assert wrap_iteration_prompt("fix typos", 1) == (
        '<loop-iteration index="1">\n' + BODY + "fix typos\n</loop-iteration>"
    )


def test_an_explicit_cap_adds_the_total_attribute_after_the_index():
    assert wrap_iteration_prompt("fix typos", 2, 5) == (
        '<loop-iteration index="2" total="5">\n' + BODY + "fix typos\n</loop-iteration>"
    )


def test_a_total_of_zero_is_still_announced():
    assert wrap_iteration_prompt("p", 1, 0).startswith('<loop-iteration index="1" total="0">\n')


def test_the_prompt_is_embedded_verbatim():
    prompt = 'line one\n\n  [bold]markup[/bold] "quoted" ünïcode 日本語\n</loop-iteration> sneaky'
    wrapped = wrap_iteration_prompt(prompt, 3, 9)
    assert f"\n\n{prompt}\n</loop-iteration>" in wrapped
    assert wrapped.endswith(f"{prompt}\n</loop-iteration>")


def test_an_empty_prompt_leaves_an_empty_body():
    assert wrap_iteration_prompt("", 1).endswith("\n\n\n</loop-iteration>")


def test_the_wrapper_itself_uses_no_square_brackets():
    """Textual reads ``[...]`` as markup; an earlier ``[/loop ...]`` wrapper crashed the TUI."""
    wrapped = wrap_iteration_prompt("p", 1, 5)
    assert "[" not in wrapped
    assert "]" not in wrapped


def test_it_coaches_the_two_legitimate_exits():
    wrapped = wrap_iteration_prompt("p", 1)
    assert "loop_set_total(N)" in wrapped
    assert "loop_stop()" in wrapped
    assert "do not ask the user" in wrapped
