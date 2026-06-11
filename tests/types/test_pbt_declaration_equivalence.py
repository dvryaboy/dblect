# pyright: reportInvalidTypeForm=false
"""Property-based contracts for the declaration algebra.

Two laws hold the authoring surface together. Spellings that fix and map the
same fields must mean the same thing regardless of route: one ``refine`` call,
a chain of them in any order, ``columns`` before or after, the call-form
sugar. And the bound tag the bridge derives from a spec must follow the
documented binding rule exactly: a fixed field pins a ``Concrete`` identity,
an open field rides as a ``PerRow`` binding on its mapped or like-named
column.
"""

from hypothesis import given
from hypothesis import strategies as st

from dblect.demo import Currency, Money
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.lineage.properties.domain_type import Concrete, Dimension, PerRow, Tagged, tagged
from dblect.types import domain_tag

_SRC = SourceRef(SourceKind.MODEL, "model.shop.stg_charges")


class Revenue(Money):
    contains_tax: bool
    contains_discount: bool


_FIXINGS = st.fixed_dictionaries(
    {},
    optional={
        "contains_tax": st.booleans(),
        "contains_discount": st.booleans(),
        "currency": st.sampled_from((Currency.USD, "USD", Currency.EUR, "EUR", Currency.GBP)),
    },
)

_COLUMN_MAPS = st.fixed_dictionaries(
    {},
    optional={
        "amount": st.sampled_from(("amount", "net_amount", "amt")),
        "contains_tax": st.just("taxed"),
        "contains_discount": st.just("discounted"),
        "currency": st.sampled_from(("currency", "currency_code")),
    },
)


@given(fixed=_FIXINGS, mapped=_COLUMN_MAPS, data=st.data())
def test_every_spelling_route_yields_one_spec(
    fixed: dict[str, object], mapped: dict[str, str], data: st.DataObject
) -> None:
    mapped = {k: v for k, v in mapped.items() if k not in fixed}
    direct = Revenue.columns(**mapped).refine(**fixed)

    ops = [("fix", k, v) for k, v in fixed.items()] + [("map", k, v) for k, v in mapped.items()]
    shuffled = data.draw(st.permutations(ops))
    chained = Revenue
    for op, name, value in shuffled:
        if op == "fix":
            chained = chained.refine(**{name: value})
        else:
            chained = chained.columns(**{name: str(value)})

    assert chained.spec() == direct.spec()


@given(fixed=_FIXINGS, mapped=_COLUMN_MAPS)
def test_call_form_agrees_with_refine_and_columns(
    fixed: dict[str, object], mapped: dict[str, str]
) -> None:
    mapped = {k: v for k, v in mapped.items() if k not in fixed}
    direct = Revenue.columns(**mapped).refine(**fixed)

    # The call form can spell every fixing plus the magnitude's column mapping;
    # companion column maps stay with .columns().
    call_kwargs: dict[str, object] = dict(fixed)
    companion_maps = dict(mapped)
    if "amount" in companion_maps:
        call_kwargs["amount"] = companion_maps.pop("amount")
    called = Revenue.columns(**companion_maps)(**call_kwargs)

    assert called.spec() == direct.spec()


@given(fixed=_FIXINGS, mapped=_COLUMN_MAPS)
def test_bound_tag_follows_the_binding_rule(
    fixed: dict[str, object], mapped: dict[str, str]
) -> None:
    mapped = {k: v for k, v in mapped.items() if k not in fixed}
    spec = Revenue.columns(**mapped).refine(**fixed).spec()

    bound = domain_tag(spec, _SRC)
    assert bound is not None
    assert bound.column == ColumnRef(_SRC, mapped.get("amount", "amount"))
    assert isinstance(bound.tag, Tagged)  # a single declaration can never conflict

    def coordinate(name: str) -> Concrete | PerRow:
        if name in fixed:
            return Concrete(str(fixed[name]).casefold())
        return PerRow(ColumnRef(_SRC, mapped.get(name, name)))

    expected = tagged(
        dimension=Dimension.of(coordinate("currency")),
        nominal={
            "contains_tax": coordinate("contains_tax"),
            "contains_discount": coordinate("contains_discount"),
        },
    )
    assert bound.tag == expected
