"""Keyword DSL tests — parser shape + evaluator semantics."""

from __future__ import annotations

import pytest

from socmon.detectors._keyword_dsl import Op, Term, evaluate, parse


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_single_bare_term() -> None:
    assert parse("acme") == [Term("acme", False)]


def test_parse_quoted_phrase() -> None:
    p = parse('"acme corp"')
    assert p == [Term("acme corp", False)]


def test_parse_and_or_chain() -> None:
    p = parse("acme AND breach OR leak")
    assert p == [
        Term("acme", False),
        Op("AND"),
        Term("breach", False),
        Op("OR"),
        Term("leak", False),
    ]


def test_parse_not_prefix() -> None:
    p = parse("acme AND NOT marketing")
    assert p == [
        Term("acme", False),
        Op("AND"),
        Term("marketing", True),
    ]


def test_parse_near_with_distance() -> None:
    p = parse('"acme" NEAR/10 credentials')
    assert p == [
        Term("acme", False),
        Op("NEAR", distance=10),
        Term("credentials", False),
    ]


def test_parse_lowercases_terms() -> None:
    p = parse('"ACME Corp"')
    assert p[0].text == "acme corp"


@pytest.mark.parametrize("expr,err", [
    ("", "empty"),
    ("   ", "empty"),
    ("AND foo", "must start with a term"),
    ("foo AND", "cannot end with an operator"),
    ("foo bar", "missing operator"),       # two adjacent terms
    ("foo AND OR bar", "duplicate"),        # two adjacent ops
    ("NOT", "dangling NOT"),
    ("NOT AND", "must be followed by a term"),
])
def test_parse_errors(expr: str, err: str) -> None:
    with pytest.raises(ValueError, match=err):
        parse(expr)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def E(expr: str, text: str) -> bool:
    return evaluate(parse(expr), text)


def test_eval_single_term() -> None:
    assert E("acme", "Acme Corp announced...") is True
    assert E("acme", "Random news") is False


def test_eval_quoted_phrase() -> None:
    assert E('"acme corp"', "Acme Corp had a great quarter") is True
    assert E('"acme corp"', "Acme had a great quarter") is False


def test_eval_substring_within_word() -> None:
    # Substring semantics — useful for catching "acmecorp" inside "acmecorporation".
    assert E("acme", "ACMECORPORATION") is True


def test_eval_and() -> None:
    assert E("acme AND breach", "Acme suffered a breach") is True
    assert E("acme AND breach", "Acme had good news") is False


def test_eval_or() -> None:
    assert E("breach OR leak", "data leak reported") is True
    assert E("breach OR leak", "earnings call") is False


def test_eval_not() -> None:
    assert E("acme AND NOT marketing", "acme released a security update") is True
    assert E("acme AND NOT marketing", "acme marketing campaign") is False


def test_eval_left_to_right_or_then_and() -> None:
    # Strict L-R: `breach OR leak AND acme` = `(breach OR leak) AND acme`
    # i.e. evaluator shouldn't apply usual AND-tighter precedence.
    assert E("breach OR leak AND acme", "leak at acme") is True
    assert E("breach OR leak AND acme", "leak at globex") is False
    # Without acme, even with breach OR leak, fails because of trailing AND acme.
    assert E("breach OR leak AND acme", "breach at globex") is False


def test_eval_near_within_distance() -> None:
    assert E('"acme" NEAR/3 credentials', "acme leaked customer credentials today") is True
    # 5 words apart — outside NEAR/3, inside NEAR/5
    assert E('"acme" NEAR/3 credentials',
             "acme had a quarterly review meeting yesterday and credentials were not discussed") is False
    assert E('"acme" NEAR/15 credentials',
             "acme had a quarterly review meeting yesterday and credentials were not discussed") is True


def test_eval_near_missing_term_is_false() -> None:
    assert E('"acme" NEAR/3 credentials', "credentials were leaked") is False


def test_eval_near_with_phrase() -> None:
    # Multi-word phrase positions count as the start of the run.
    assert E('"acme corp" NEAR/3 breach', "acme corp had a major breach") is True


def test_eval_chained_near_then_and() -> None:
    # acme NEAR/3 breach AND public
    assert E("acme NEAR/3 breach AND public",
             "acme suffered a breach made public yesterday") is True
    assert E("acme NEAR/3 breach AND public",
             "acme suffered a breach yesterday in private") is False


def test_eval_handles_unicode_words() -> None:
    # Confusables shouldn't matter at this layer (the impersonation detector
    # handles those); the DSL just does case-insensitive substring.
    assert E("café", "Best Café in town") is True
