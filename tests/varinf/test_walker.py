"""Rule-by-rule pins for the source-Jinja walker.

The walker is a value boundary: a template string in, a ``WalkResult`` of
immutable ``VarUsage`` records out. Each test feeds one synthetic template
exercising a single ``UsageContext`` and asserts the record that comes back, so
the tests pin the contract (what usages, with what context) and survive a
refactor of the walk's internals.
"""

from __future__ import annotations

import pytest

from dblect.varinf import (
    Arithmetic,
    ArithOp,
    ComparisonOp,
    Confidence,
    Equality,
    Inequality,
    InSet,
    MacroArg,
    SqlLiteral,
    TruthyTest,
    Unknown,
    VarKind,
    VarUsage,
    walk_source,
)
from dblect.varinf.usage import LiteralPosition


def usages(source: str) -> tuple[VarUsage, ...]:
    result = walk_source(source, unique_id="model.test.m", file_path="models/m.sql")
    assert result.parsed, f"expected a clean parse, got opaque: {result.opaque}"
    return result.usages


def one(source: str) -> VarUsage:
    found = usages(source)
    assert len(found) == 1, f"expected exactly one usage, got {found}"
    return found[0]


def test_truthy_test() -> None:
    u = one("{% if var('flag') %}a{% endif %}")
    assert u.var_name == "flag"
    assert u.var_kind is VarKind.VAR
    assert u.context == TruthyTest()


def test_truthy_test_under_boolean_op() -> None:
    u = one("{% if var('flag') and other %}a{% endif %}")
    assert u.context == TruthyTest()


def test_for_iterable_is_control_flow() -> None:
    u = one("{% for r in var('regions') %}{{ r }}{% endfor %}")
    assert u.var_name == "regions"
    assert u.context == TruthyTest()


def test_equality_string() -> None:
    u = one("{% if var('env') == 'prod' %}a{% endif %}")
    assert u.context == Equality("prod")


def test_equality_bool() -> None:
    u = one("{% if var('enabled') == true %}a{% endif %}")
    assert u.context == Equality(True)


def test_inequality_literal_negation_treated_as_equality() -> None:
    # != carries the same type/domain/control-flow signal as ==.
    u = one("{% if var('env') != 'dev' %}a{% endif %}")
    assert u.context == Equality("dev")


def test_inequality_numeric() -> None:
    u = one("{% if var('threshold') > 100 %}a{% endif %}")
    assert u.context == Inequality(100, ComparisonOp.GT)


def test_inequality_flips_when_literal_on_left() -> None:
    # 100 < var('x')  is  var('x') > 100
    u = one("{% if 100 < var('threshold') %}a{% endif %}")
    assert u.context == Inequality(100, ComparisonOp.GT)


def test_in_set() -> None:
    u = one("{% if var('region') in ['us', 'eu'] %}a{% endif %}")
    assert u.context == InSet(("us", "eu"))


def test_arithmetic() -> None:
    u = one("{{ var('n') + 1 }}")
    assert u.context == Arithmetic(ArithOp.ADD, 1)


def test_arithmetic_non_literal_other() -> None:
    u = one("{{ var('n') * other }}")
    assert u.context == Arithmetic(ArithOp.MUL, None)


def test_sql_literal() -> None:
    u = one("select * from t limit {{ var('row_limit') }}")
    assert u.context == SqlLiteral(LiteralPosition.UNKNOWN)


def test_macro_arg() -> None:
    u = one("{{ get_flag(var('include_tax')) }}")
    assert u.context == MacroArg(macro="get_flag", position=0)


def test_unknown_position() -> None:
    # A var behind a filter is recognized but not classified further.
    u = one("{{ var('x') | upper }}")
    assert u.context == Unknown()


def test_env_var_kind() -> None:
    u = one("{% if env_var('DEBUG') == 'true' %}a{% endif %}")
    assert u.var_name == "DEBUG"
    assert u.var_kind is VarKind.ENV_VAR
    assert u.context == Equality("true")


def test_inline_default_does_not_break_name() -> None:
    u = one("{{ var('schema', 'analytics') }}")
    assert u.var_name == "schema"
    assert u.var_kind is VarKind.VAR


def test_nested_var_in_inline_default_is_found() -> None:
    # var('a', var('b')) carries a second var in its default; both are discovered.
    found = usages("{{ var('a', var('b')) }}")
    assert {u.var_name for u in found} == {"a", "b"}


def test_dynamic_var_name_is_skipped() -> None:
    # A non-constant var name is not knowable statically; the walker emits nothing
    # rather than keying a usage by a name it does not have.
    assert usages("{{ var(some_expr) }}") == ()


def test_location_carries_file_and_line() -> None:
    u = one("\n\n{% if var('flag') %}a{% endif %}")
    assert u.location.file == "models/m.sql"
    assert u.location.line == 3
    assert u.confidence is Confidence.FULL


def test_multiple_usages_each_recorded() -> None:
    found = usages("{% if var('a') %}{{ var('b') }}{% endif %}")
    by_name = {u.var_name: u.context for u in found}
    assert by_name == {"a": TruthyTest(), "b": SqlLiteral(LiteralPosition.UNKNOWN)}


def test_var_inside_snapshot_keeps_control_flow_context() -> None:
    source = (
        "{% snapshot orders_snapshot %}"
        "{% if var('full_refresh') %}select 1{% else %}select 2{% endif %}"
        "{% endsnapshot %}"
    )
    u = one(source)
    assert u.var_name == "full_refresh"
    assert u.context == TruthyTest()


def test_unparseable_body_degrades_to_opaque() -> None:
    result = walk_source("{% bogus_tag %}x{% endbogus_tag %}", unique_id="model.test.m")
    assert not result.parsed
    assert result.opaque is not None
    assert result.opaque.unique_id == "model.test.m"
    assert result.usages == ()


@pytest.mark.parametrize(
    ("op_text", "expected"),
    [
        ("<", ComparisonOp.LT),
        (">", ComparisonOp.GT),
        ("<=", ComparisonOp.LTEQ),
        (">=", ComparisonOp.GTEQ),
    ],
)
def test_each_ordering_operator(op_text: str, expected: ComparisonOp) -> None:
    u = one(f"{{% if var('x') {op_text} 5 %}}a{{% endif %}}")
    assert u.context == Inequality(5, expected)
