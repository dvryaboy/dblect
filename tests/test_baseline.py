"""Unit tests for the base-manifest finding diff, with no manifest compiled.

``introduced_findings`` is pure over two finding sequences, so its contract is pinned
here directly: a finding present in the base under its cross-world identity is
preexisting and drops; one present only in HEAD is introduced and survives. The
load-bearing property, the one a source-line diff cannot offer, is that the identity
ignores line span and message, so a finding that merely moved or was reworded between
the two compilations reads as preexisting. The end-to-end CLI behaviour over real
manifests lives in ``tests/cli/test_check_baseline.py``.
"""

from __future__ import annotations

from dblect.audit.walker import LocatedFinding
from dblect.baseline import introduced_findings
from dblect.check.findings import CheckFinding, CheckFindingKind
from dblect.sql.patterns import Finding, FindingKind


def _structural(
    model: str,
    snippet: str,
    *,
    line_start: int = 1,
    line_end: int = 1,
    message: str = "hazard",
) -> LocatedFinding:
    return LocatedFinding(
        model_unique_id=model,
        file_path=f"models/{model.split('.')[-1]}.sql",
        finding=Finding(
            kind=FindingKind.NULL_GROUP_AFTER_OUTER_JOIN,
            message=message,
            sql_snippet=snippet,
            line_start=line_start,
            line_end=line_end,
        ),
    )


def _declaration(model: str, *, column: str, contract: str) -> CheckFinding:
    return CheckFinding(
        kind=CheckFindingKind.DOMAIN_TYPE_CONTRADICTION,
        message="contradiction",
        model_unique_id=model,
        column=column,
        contract=contract,
    )


def test_empty_base_keeps_every_head_finding() -> None:
    head = (
        _structural("model.p.a", "orders.id"),
        _declaration("model.p.b", column="x", contract="C"),
    )
    assert introduced_findings(head, ()) == head


def test_preexisting_structural_finding_is_dropped() -> None:
    f = _structural("model.p.a", "orders.id")
    assert introduced_findings((f,), (f,)) == ()


def test_structural_finding_dropped_despite_moved_line() -> None:
    # The identity ignores the line span, so the same hazard at a different compiled
    # line is preexisting. This is the property a source-line diff cannot honor.
    head = (_structural("model.p.a", "orders.id", line_start=44, line_end=44),)
    base = (_structural("model.p.a", "orders.id", line_start=12, line_end=12),)
    assert introduced_findings(head, base) == ()


def test_structural_finding_dropped_despite_reworded_message() -> None:
    head = (_structural("model.p.a", "orders.id", message="now phrased differently"),)
    base = (_structural("model.p.a", "orders.id", message="hazard"),)
    assert introduced_findings(head, base) == ()


def test_new_snippet_on_same_model_is_introduced() -> None:
    base = (_structural("model.p.a", "orders.id"),)
    new = _structural("model.p.a", "payments.id")
    assert introduced_findings((*base, new), base) == (new,)


def test_same_snippet_on_a_different_model_is_introduced() -> None:
    base = (_structural("model.p.a", "orders.id"),)
    downstream = _structural("model.p.b", "orders.id")
    assert introduced_findings((*base, downstream), base) == (downstream,)


def test_declaration_finding_preexisting_is_dropped() -> None:
    f = _declaration("model.p.a", column="amount", contract="Money")
    assert introduced_findings((f,), (f,)) == ()


def test_declaration_finding_with_changed_contract_is_introduced() -> None:
    base = (_declaration("model.p.a", column="amount", contract="Money"),)
    changed = _declaration("model.p.a", column="amount", contract="USD")
    assert introduced_findings((changed,), base) == (changed,)


def test_declaration_finding_with_changed_column_is_introduced() -> None:
    base = (_declaration("model.p.a", column="amount", contract="Money"),)
    changed = _declaration("model.p.a", column="revenue", contract="Money")
    assert introduced_findings((changed,), base) == (changed,)


def test_families_do_not_collide() -> None:
    # A structural and a declaration finding can never share an identity (the family
    # tag leads the tuple), so a base full of one family never masks the other.
    struct = _structural("model.p.a", "orders.id")
    decl = _declaration("model.p.a", column="amount", contract="Money")
    assert introduced_findings((struct,), (decl,)) == (struct,)
    assert introduced_findings((decl,), (struct,)) == (decl,)


def test_fixed_finding_never_appears() -> None:
    # A finding in the base but not in HEAD (one the change fixed) is simply absent
    # from the introduced set; the diff reports introduced findings, not fixed ones.
    base = (_structural("model.p.a", "orders.id"), _structural("model.p.b", "x.y"))
    head = (_structural("model.p.a", "orders.id"),)
    assert introduced_findings(head, base) == ()
