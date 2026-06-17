"""Property-based tests for the walker over generated valid Jinja.

The hand-written rule tests pin context classification on templates we chose. A
generator instead produces structurally varied valid Jinja (nested arithmetic,
comparisons, conditionals, filters, macro calls, lists, snapshots) to flush out
shapes that parse cleanly but the walker mishandles.

The properties use oracles independent of the walker's recursion:

* **Completeness.** Every static ``var()`` / ``env_var()`` call in a parseable
  template is discovered. The oracle is a flat ``find_all`` over the parsed AST,
  so it shares no logic with the context-carrying walk: the walk may classify a
  usage however it likes, but it may not lose one.
* **Totality.** ``walk_source`` returns a ``WalkResult`` for any input at all,
  degrading a parse failure to an opaque diagnostic rather than raising.
"""

from __future__ import annotations

from collections import Counter

import hypothesis.strategies as st
from hypothesis import given, settings
from jinja2 import nodes

from dblect.varinf import VarKind, WalkResult, make_environment, walk_source

# Leaf expressions. Var/env_var calls always carry a constant name, the only
# shape the walker keys a usage on (and the only shape the oracle counts).
_VAR_NAMES = ("a", "b", "flag", "threshold", "region")
_ENV_NAMES = ("DEBUG", "MODE")
_literals = st.sampled_from(["'p'", "'q'", "1", "2", "3.5", "true", "false"])
_atoms = st.one_of(
    _literals,
    st.sampled_from(["row", "items", "x"]),
    st.sampled_from(_VAR_NAMES).map(lambda n: f"var('{n}')"),
    st.sampled_from(_ENV_NAMES).map(lambda n: f"env_var('{n}')"),
)


def _extend(children: st.SearchStrategy[str]) -> st.SearchStrategy[str]:
    # ``.map`` over typed tuples/lists keeps the callback parameters typed (``st.builds``
    # erases them to unknown under strict typing).
    binops = st.sampled_from(["+", "-", "*", "==", "!=", ">", "<", ">=", "<=", "and", "or"])
    return st.one_of(
        st.tuples(children, binops, children).map(lambda t: f"({t[0]} {t[1]} {t[2]})"),
        children.map(lambda c: f"(not {c})"),
        children.map(lambda c: f"({c} | upper)"),
        st.tuples(children, children, children).map(lambda t: f"({t[0]} if {t[1]} else {t[2]})"),
        st.lists(children, min_size=1, max_size=3).map(lambda xs: "f(" + ", ".join(xs) + ")"),
        # ``*args`` splat: a var inside reaches the call through dyn_args, the path the
        # plain positional arm does not exercise.
        st.lists(children, min_size=1, max_size=2).map(lambda xs: "f(*[" + ", ".join(xs) + "])"),
        st.lists(children, min_size=1, max_size=3).map(lambda xs: "[" + ", ".join(xs) + "]"),
        st.tuples(children, st.lists(_literals, min_size=1, max_size=3)).map(
            lambda t: f"({t[0]} in [" + ", ".join(t[1]) + "])"
        ),
        # A var carrying a nested expression as its inline default. This places a
        # var (and any var the default holds) under every outer position the
        # recursion reaches, the shape that exposed the inline-default drop.
        st.tuples(st.sampled_from(_VAR_NAMES), children).map(lambda t: f"var('{t[0]}', {t[1]})"),
    )


_exprs = st.recursive(_atoms, _extend, max_leaves=10)


_templates = st.lists(
    st.one_of(
        _exprs.map(lambda e: "{{ " + e + " }}"),
        _exprs.map(lambda e: "{% if " + e + " %}select 1{% endif %}"),
        st.tuples(_exprs, _exprs).map(
            lambda t: "{% if " + t[0] + " %}a{% else %}{{ " + t[1] + " }}{% endif %}"
        ),
        _exprs.map(lambda e: "{% for it in " + e + " %}{{ it }}{% endfor %}"),
        _exprs.map(lambda e: "{% set q = " + e + " %}"),
        _exprs.map(lambda e: "{% snapshot s %}{% if " + e + " %}x{% endif %}{% endsnapshot %}"),
    ),
    min_size=1,
    max_size=4,
).map(lambda parts: "\nselect 1\n".join(parts))


def _ground_truth(source: str) -> Counter[tuple[str, str]]:
    """The (kind, name) multiset of static var/env_var calls, by a flat AST scan
    that shares no logic with the walker's context-carrying recursion."""
    template = make_environment().parse(source)
    found: Counter[tuple[str, str]] = Counter()
    for call in template.find_all(nodes.Call):
        callee = call.node
        if not isinstance(callee, nodes.Name) or callee.name not in ("var", "env_var"):
            continue
        if call.args and isinstance(call.args[0], nodes.Const):
            value = call.args[0].value
            if isinstance(value, str):
                found[(callee.name, value)] += 1
    return found


@settings(max_examples=300, deadline=None)
@given(_templates)
def test_walker_discovers_every_static_var(source: str) -> None:
    result = walk_source(source, unique_id="model.test.m", file_path="models/m.sql")
    assert result.parsed, f"generator emitted unparseable Jinja: {source!r} -> {result.opaque}"
    walked: Counter[tuple[str, str]] = Counter(
        (u.var_kind.value, u.var_name) for u in result.usages
    )
    assert walked == _ground_truth(source), source


# Each position classifies the outer var through a specialized emit (comparison or
# membership against a literal, arithmetic, a macro argument) that does not descend
# the var's own arguments, so each is a place an inline-default var can be dropped.
# ``$E`` marks the slot, replaced by hand since Jinja's braces rule out str.format.
_DEFAULT_BEARING_POSITIONS = (
    "{{ $E }}",
    "{% if $E == 'x' %}y{% endif %}",
    "{% if $E != 'x' %}y{% endif %}",
    "{% if $E > 5 %}y{% endif %}",
    "{% if $E in ['x'] %}y{% endif %}",
    "{{ $E + 1 }}",
    "{{ f($E) }}",
    "{{ f(k=$E) }}",
)


@given(
    outer=st.sampled_from(_VAR_NAMES),
    inner=st.sampled_from(_VAR_NAMES),
    template=st.sampled_from(_DEFAULT_BEARING_POSITIONS),
)
def test_inline_default_var_survives_every_outer_position(
    outer: str, inner: str, template: str
) -> None:
    """A var in an inline default is discovered no matter the outer var's position.

    The broad completeness generator produces this shape only by chance, so this
    property exercises it directly: an outer var carrying a nested default var,
    dropped into each position that classifies the outer var through a specialized
    emit. Checked against the same flat ``find_all`` oracle, with multiplicity, so
    ``var('a', var('a'))`` must surface twice.
    """
    source = template.replace("$E", f"var('{outer}', var('{inner}'))")
    result = walk_source(source, unique_id="model.test.m")
    assert result.parsed, source
    walked: Counter[tuple[str, str]] = Counter(
        (u.var_kind.value, u.var_name) for u in result.usages
    )
    assert walked == _ground_truth(source), source


@settings(max_examples=300, deadline=None)
@given(st.text(alphabet=st.characters(codec="utf-8"), max_size=80))
def test_walk_source_is_total_on_arbitrary_input(source: str) -> None:
    # Arbitrary text never crashes the walker: it either parses or degrades to an
    # opaque diagnostic. VarKind is referenced to keep the contract explicit.
    result = walk_source(source, unique_id="model.test.m")
    assert isinstance(result, WalkResult)
    assert result.parsed == (result.opaque is None)
    assert all(isinstance(u.var_kind, VarKind) for u in result.usages)
