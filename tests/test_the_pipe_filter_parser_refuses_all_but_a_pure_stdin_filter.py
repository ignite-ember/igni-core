"""The argument parser behind the pipe-filter list, one corner at a time.

``test_a_filter_after_a_pipe_needs_no_rule`` pins the list through the
evaluator. This file pins the parser: unparseable input, ``--``, the
three-character boundary of long-option prefixes, how option values are
skipped inside short clusters, patterns that use up the positional
allowance, and each refused option the evaluator test does not name.
"""

from __future__ import annotations

import pytest

from igni_core.core.config.permission_eval.pipe_filters import (
    PIPE_FILTERS,
    is_pipe_filter,
    stdin_filter,
)


class TestWhatIsNotACommandLine:
    @pytest.mark.parametrize("command", ["", "   ", "\t"])
    def test_nothing_is_not_a_filter(self, command):
        assert is_pipe_filter(command) is False

    @pytest.mark.parametrize("command", ["grep 'foo", 'grep "foo', "grep foo\\"])
    def test_unbalanced_quoting_is_refused_not_raised(self, command):
        assert is_pipe_filter(command) is False

    @pytest.mark.parametrize("command", ["/usr/bin/tail", "TAIL", "Head -n 3", "./wc", "cat"])
    def test_only_the_bare_listed_lowercase_name_counts(self, command):
        assert is_pipe_filter(command) is False

    @pytest.mark.parametrize("name", sorted(PIPE_FILTERS))
    def test_a_listed_command_with_no_arguments_filters_stdin(self, name):
        assert is_pipe_filter(name) is True

    def test_quoting_is_resolved_before_the_arguments_are_judged(self):
        assert is_pipe_filter("grep 'a b'") is True
        assert is_pipe_filter("grep 'a b' 'c d'") is False


class TestDoubleDash:
    def test_it_ends_the_options_so_a_refused_name_after_it_is_an_operand(self):
        assert is_pipe_filter("grep -- -r") is True
        assert is_pipe_filter("grep -- -r file") is False

    def test_everything_after_it_is_counted_as_operands(self):
        assert is_pipe_filter("head -- a") is False
        assert is_pipe_filter("tr -- a b") is True
        assert is_pipe_filter("tr -- a b c") is False

    def test_nothing_after_it_adds_nothing(self):
        assert is_pipe_filter("head --") is True

    def test_a_refused_option_before_it_still_refuses(self):
        assert is_pipe_filter("grep -r -- x") is False


class TestALoneDash:
    def test_it_is_an_operand_not_an_option(self):
        assert is_pipe_filter("uniq -") is False
        assert is_pipe_filter("uniq - out.txt") is False
        assert is_pipe_filter("sort -") is False
        assert is_pipe_filter("grep x -") is False
        assert is_pipe_filter("grep x") is True


class TestLongOptionAbbreviations:
    def test_a_prefix_of_a_refused_long_option_is_that_option(self):
        assert is_pipe_filter("sort --outp=x") is False
        assert is_pipe_filter("sort --output=x") is False
        assert is_pipe_filter("sort --compress") is False
        assert is_pipe_filter("tail --foll") is False
        assert is_pipe_filter("rg --search") is False

    def test_the_boundary_is_more_than_three_characters(self):
        # the name counts its dashes: '--ou' is four characters, a prefix of --output
        assert is_pipe_filter("sort --ou") is False
        # '--f' is three, so it is never read as --file
        assert is_pipe_filter("rg --fi") is False
        assert is_pipe_filter("rg --f") is True

    def test_a_known_option_is_itself_not_an_abbreviation_of_a_refused_one(self):
        assert is_pipe_filter("jq --arg n v .") is True
        assert is_pipe_filter("jq --args") is False
        assert is_pipe_filter("jq --argj n 1 .") is True
        assert is_pipe_filter("jq --jsonargs") is False

    def test_an_unknown_long_option_is_ignored_not_counted(self):
        assert is_pipe_filter("head --quiet") is True
        assert is_pipe_filter("wc --lines") is True


class TestOptionValues:
    def test_a_short_option_value_is_not_an_operand(self):
        assert is_pipe_filter("head -n 5") is True
        assert is_pipe_filter("head -n5") is True
        assert is_pipe_filter("head -n 5 file") is False

    def test_a_long_option_takes_its_value_attached_or_next(self):
        assert is_pipe_filter("head --lines 5") is True
        assert is_pipe_filter("head --lines=5") is True
        assert is_pipe_filter("head --lines=5 file") is False
        assert is_pipe_filter("head --lines 5 file") is False

    def test_an_option_that_needs_a_value_may_be_last(self):
        assert is_pipe_filter("head -n") is True
        assert is_pipe_filter("head --lines") is True

    def test_a_value_letter_ending_a_cluster_swallows_the_next_argument(self):
        assert is_pipe_filter("grep -nA 3 x") is True
        assert is_pipe_filter("grep -nA 3") is True
        assert is_pipe_filter("grep -nA 3 x y") is False

    def test_a_value_letter_in_mid_cluster_takes_the_rest_as_its_value(self):
        # -A3n: the value of -A is '3n'; nothing after it is skipped. Likewise
        # 'grep -An 3 x' is two operands, '3' and 'x'.
        assert is_pipe_filter("grep -An 3 x") is False
        assert is_pipe_filter("grep -A3n x") is True
        assert is_pipe_filter("grep -A3n x y") is False

    def test_a_refused_letter_anywhere_before_the_value_refuses(self):
        assert is_pipe_filter("sort -rno x") is False
        assert is_pipe_filter("sort -no") is False
        assert is_pipe_filter("sort -rn") is True
        assert is_pipe_filter("tail -fn 5") is False
        assert is_pipe_filter("tail -nf") is True  # 'f' is the value of -n
        assert is_pipe_filter("grep -rn x") is False

    def test_numeric_options_are_not_refused(self):
        assert is_pipe_filter("tail -20") is True
        assert is_pipe_filter("head -5") is True

    def test_two_value_options_swallow_two_arguments(self):
        assert is_pipe_filter("jq --arg name value .x") is True
        assert is_pipe_filter("jq --argjson n 1 --arg a b .x") is True
        assert is_pipe_filter("jq --arg name value .x file") is False
        assert is_pipe_filter("jq --arg name") is True

    def test_an_equals_value_does_not_swallow_the_next_argument(self):
        assert is_pipe_filter("grep --context=2 x y") is False
        assert is_pipe_filter("grep --context 2 x") is True


class TestPatternsUseUpTheAllowance:
    @pytest.mark.parametrize("name", ["grep", "egrep", "fgrep", "rg"])
    def test_a_pattern_option_and_a_positional_would_be_a_file(self, name):
        assert is_pipe_filter(f"{name} foo") is True
        assert is_pipe_filter(f"{name} -e foo") is True
        assert is_pipe_filter(f"{name} -e foo bar") is False
        assert is_pipe_filter(f"{name} foo bar") is False

    @pytest.mark.parametrize("name", ["grep", "rg"])
    def test_several_patterns_are_fine(self, name):
        assert is_pipe_filter(f"{name} -e foo -e bar") is True
        assert is_pipe_filter(f"{name} --regexp foo --regexp=bar") is True
        assert is_pipe_filter(f"{name} --regexp foo bar") is False

    def test_the_pattern_flag_without_a_pattern_still_uses_the_allowance(self):
        assert is_pipe_filter("grep -e") is True
        assert is_pipe_filter("grep -e x y") is False

    def test_a_filter_with_no_positionals_to_spend_does_not_gain_one(self):
        assert stdin_filter(pattern_short="e")(["-e", "x"]) is True
        assert stdin_filter(pattern_short="e")(["x"]) is False


class TestRefusedOptionsOneByOne:
    @pytest.mark.parametrize(
        "command",
        [
            "rg --pre-glob '*.gz' x",
            "rg --pre cat x",
            "rg --search-zip x",
            "rg -z x",
            "rg --file pats",
            "rg -f pats",
            "rg --files",
            "tail --pid=1",
            "tail --retry",
            "tail -F",
            "tail -f",
            "tail --follow=name",
            "sort --files0-from=f",
            "sort --random-source=f",
            "sort -o out",
            "sort --compress-program=sh",
            "wc --files0-from=f",
            "grep --include='*.c' x",
            "grep --exclude='*.c' x",
            "grep --exclude-from=f x",
            "grep --exclude-dir=d x",
            "grep --recursive x",
            "grep --dereference-recursive x",
            "grep --directories=recurse x",
            "grep --devices=read x",
            "grep --file=f",
            "grep -R x",
            "grep -d recurse x",
            "grep -D read x",
            "grep -f pats",
            "egrep -r x",
            "fgrep -r x",
            "jq -f prog.jq",
            "jq -L dir .",
            "jq --from-file prog.jq",
            "jq --slurpfile a f .",
            "jq --rawfile a f .",
            "jq --jsonargs",
        ],
    )
    def test_each_is_not_a_pure_filter(self, command):
        assert is_pipe_filter(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            "sort -r -n -u",
            "sort -t , -k 2,2",
            "sort --key=2 --field-separator=,",
            "uniq -c -f 1 -s 2 -w 3",
            "uniq --skip-fields 1",
            "cut -d: -f1",
            "cut --delimiter , --fields 2",
            "nl -b a -w 3",
            "tr -d x",
            "tr a-z A-Z",
            "wc -l",
            "rg -n -C 2 -g '*.py' -t py foo",
            "rg --max-count 1 foo",
            "tail -n +2",
            "tail -c 10 -s 2",
            "jq -r .a",
            "jq --indent 2 .",
            "jq -n 'input'",
        ],
    )
    def test_each_is_a_pure_filter(self, command):
        assert is_pipe_filter(command) is True

    def test_a_third_tr_set_is_a_file_operand(self):
        assert is_pipe_filter("tr a b c") is False


class TestTheCheckerBuilderDirectly:
    def test_defaults_accept_only_flags(self):
        check = stdin_filter()

        assert check([]) is True
        assert check(["-x", "--anything"]) is True
        assert check(["file"]) is False

    def test_max_positionals_bounds_the_operands(self):
        check = stdin_filter(max_positionals=2)

        assert check(["a", "b"]) is True
        assert check(["a", "b", "c"]) is False

    def test_value_options_are_skipped_and_refused_ones_refuse(self):
        check = stdin_filter(
            short_values="n", long_values=("--num",), refuse_short="x", refuse_long=("--bad",)
        )

        assert check(["-n", "1", "--num", "2"]) is True
        assert check(["-n", "1", "file"]) is False
        assert check(["-ax"]) is False
        assert check(["--bad"]) is False
        assert check(["--bad=1"]) is False
