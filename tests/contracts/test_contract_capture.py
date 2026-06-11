"""``@contract`` capture and fact/predicate dispatch.

A marked method is run once over a self-proxy and recorded as its AST. The kind
the framework treats it as, a fact it reads or a predicate it runs, is the type
of node the body returned, nothing the author restates.
"""

from __future__ import annotations

import pytest

from dblect.contracts import (
    ContractError,
    ContractSelf,
    ast,
    capture,
    contract,
    is_contract,
    models,
)


def test_marker_is_detectable() -> None:
    @contract
    def m(self: ContractSelf) -> object:
        return self.a.determines(self.b)

    assert is_contract(m)
    assert not is_contract(lambda: None)


def test_capture_records_a_fact() -> None:
    @contract
    def country_sets_currency(self: ContractSelf) -> object:
        return self.country.determines(self.currency)

    cap = capture("country_sets_currency", country_sets_currency)
    assert cap.is_fact
    assert not cap.is_predicate
    assert cap.result == ast.DeterminesFact((ast.Col(None, "country"),), ast.Col(None, "currency"))


def test_capture_records_a_predicate() -> None:
    @contract
    def total_reconciles(self: ContractSelf) -> object:
        return (
            self.order_total.sum().group_by(self.order_id)
            == models.stg_order_items.subtotal.sum().group_by(models.stg_order_items.order_id)
        ).within(0.01)

    cap = capture("total_reconciles", total_reconciles)
    assert cap.is_predicate
    assert isinstance(cap.result, ast.Compare)
    assert cap.result.tolerance == ast.Tolerance(0.01)


def test_capture_rejects_a_non_proxy_return() -> None:
    @contract
    def broken(self: ContractSelf) -> object:
        return 42

    with pytest.raises(ContractError):
        capture("broken", broken)
