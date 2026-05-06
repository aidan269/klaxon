"""Tiny keyword expression DSL — strict left-to-right, no parens, no precedence.

Grammar (informal):
    expr      := operand (op operand)*
    op        := AND | OR | NEAR/<N>
    operand   := [NOT] term
    term      := bareword | "quoted phrase"

Examples (and what they mean under strict L-R):
    'acme'                       -> matches if "acme" appears
    '"acme corp" AND breach'     -> "acme corp" AND "breach"
    'acme AND breach OR leak'    -> ((acme AND breach) OR leak)   ← order matters!
    '"acme" NEAR/10 credentials' -> both terms within 10 words

Constraints:
  - NEAR/N requires bare/quoted terms on both sides.
  - NOT may only prefix a term (no NOT NOT, no NOT(expr)).
  - There are NO parentheses. Use multiple `Keyword` entries in YAML if you need
    OR-of-ANDs.

Why this minimal: the user explicitly opted into strict L-R for v1. Complex
boolean logic is the kind of thing that quietly grows into a bug factory; if
ops realize they need it, we can graduate to a proper precedence parser.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class Term:
    text: str   # lowercased
    negate: bool


@dataclass(frozen=True)
class Op:
    name: str          # "AND" | "OR" | "NEAR"
    distance: int = 0  # set for NEAR


# A parsed expression is a flat alternating list: [Term, Op, Term, Op, Term, ...]
ParsedExpr = list  # list[Term | Op]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


_NEAR_RE = re.compile(r"^NEAR/(\d+)$")


def parse(expr: str) -> ParsedExpr:
    """Tokenize and validate. Raises ValueError on malformed input."""
    if not expr.strip():
        raise ValueError("empty expression")

    # shlex.split with posix=True respects quoted phrases and strips the quotes.
    raw = shlex.split(expr, posix=True)
    out: list = []
    i = 0
    while i < len(raw):
        tok = raw[i]
        if tok == "AND":
            out.append(Op("AND"))
            i += 1
        elif tok == "OR":
            out.append(Op("OR"))
            i += 1
        elif (m := _NEAR_RE.match(tok)):
            out.append(Op("NEAR", distance=int(m.group(1))))
            i += 1
        elif tok == "NOT":
            if i + 1 >= len(raw):
                raise ValueError("dangling NOT at end of expression")
            nxt = raw[i + 1]
            if _is_operator(nxt):
                raise ValueError(f"NOT must be followed by a term, got {nxt!r}")
            out.append(Term(text=nxt.lower(), negate=True))
            i += 2
        else:
            out.append(Term(text=tok.lower(), negate=False))
            i += 1

    _validate_alternation(out)
    return out


def _is_operator(tok: str) -> bool:
    return tok in ("AND", "OR", "NOT") or bool(_NEAR_RE.match(tok))


def _validate_alternation(parsed: list) -> None:
    if not parsed:
        raise ValueError("empty expression")
    if not isinstance(parsed[0], Term):
        raise ValueError("expression must start with a term")
    if isinstance(parsed[-1], Op):
        raise ValueError("expression cannot end with an operator")
    for idx in range(len(parsed) - 1):
        a, b = parsed[idx], parsed[idx + 1]
        if isinstance(a, Term) == isinstance(b, Term):
            raise ValueError(
                f"missing operator or duplicate operator at position {idx}"
            )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


_WORD_RE = re.compile(r"\w+", re.UNICODE)


def evaluate(parsed: ParsedExpr, text: str) -> bool:
    """Strict left-to-right evaluation against `text`.

    NEAR semantics: requires both adjacent operand TERMS to appear in `text`
    within `distance` words of each other. The previous boolean state still
    participates: `(prior result) AND (LHS NEAR/N RHS)`. So
    `acme AND breach NEAR/5 leak` evaluates as
    `(acme AND breach) AND (breach NEAR/5 leak)`.
    """
    if not parsed:
        return False
    text_l = text.lower()
    words = _WORD_RE.findall(text_l)

    # Single term short-circuit.
    if len(parsed) == 1:
        return _term_match(parsed[0], text_l)

    result = _term_match(parsed[0], text_l)
    last_term: Term = parsed[0]

    i = 1
    while i < len(parsed):
        op: Op = parsed[i]
        rhs: Term = parsed[i + 1]
        if op.name == "AND":
            result = result and _term_match(rhs, text_l)
        elif op.name == "OR":
            result = result or _term_match(rhs, text_l)
        elif op.name == "NEAR":
            result = result and _near_match(last_term, rhs, op.distance, words)
        else:  # pragma: no cover  — parse() guards this
            raise ValueError(f"unknown op {op.name!r}")
        last_term = rhs
        i += 2

    return result


def _term_match(term: Term, text_l: str) -> bool:
    """Substring match (whole-word match would be stricter; substring is what
    most ops actually want — partial brand mentions like 'acmecorp' should
    match 'acmecorporation' too)."""
    matched = term.text in text_l
    return matched != term.negate


def _near_match(a: Term, b: Term, distance: int, words: list[str]) -> bool:
    """True if any spans of `a` and `b` are within `distance` words of each
    other (edge-to-edge — measures the gap BETWEEN the spans, not start-to-
    start). For single-word terms this is just |position_a − position_b|;
    for phrases like "acme corp" the end of the phrase is used as the edge.

    Negated terms in NEAR are nonsensical (NOT-near-X is hard to define), so
    we ignore negation in NEAR operands; use AND NOT outside if you need it.
    """
    a_spans = _spans(a.text, words)
    b_spans = _spans(b.text, words)
    if not a_spans or not b_spans:
        return False
    for a_start, a_end in a_spans:
        for b_start, b_end in b_spans:
            # Edge-to-edge gap: words strictly BETWEEN the two spans.
            gap = max(b_start - a_end - 1, a_start - b_end - 1, 0)
            if gap <= distance:
                return True
    return False


def _spans(term_text: str, words: list[str]) -> list[tuple[int, int]]:
    """All inclusive (start, end) spans where `term_text` occurs. For a single
    word that's (i, i); for a phrase of length N, (i, i+N-1)."""
    if not term_text:
        return []
    term_words = _WORD_RE.findall(term_text)
    if not term_words:
        return []
    n = len(term_words)
    out: list[tuple[int, int]] = []
    for i in range(len(words) - n + 1):
        if words[i:i + n] == term_words:
            out.append((i, i + n - 1))
    return out
