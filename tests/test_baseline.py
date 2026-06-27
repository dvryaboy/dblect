"""Unit tests for the base-manifest finding diff, with no manifest compiled.

``introduced_findings`` is a set difference keyed by ``cross_world_identity``: a
finding present in the base under that identity is preexisting and drops; one present
only in HEAD survives. The load-bearing property, the reason this is not a naive
whole-finding equality, is that the identity excludes the line span and message that
drift between two compilations, so a finding that merely moved or was reworded reads
as preexisting. The field composition of the identity itself is ``analysis``'s
contract, not this module's; here we pin only the difference. The end-to-end CLI
behaviour over real manifests lives in ``tests/cli/test_check_baseline.py``.
"""

from __future__ import annotations

from dblect.audit.walker import LocatedFinding
from dblect.baseline import introduced_findings
from dblect.check.findings import CheckFinding, CheckFindingKind
from dblect.sql import Finding, FindingKind


def _structural(
    model: str, snippet: str, *, line: int = 1, message: str = "hazard"
) -> LocatedFinding:
    return LocatedFinding(
        model_unique_id=model,
        file_path=f"models/{model.split('.')[-1]}.sql",
        finding=Finding(
            kind=FindingKind.NULL_GROUP_AFTER_OUTER_JOIN,
            message=message,
            sql_snippet=snippet,
            line_start=line,
            line_end=line,
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


def test_preexisting_finding_is_dropped() -> None:
    f = _structural("model.p.a", "orders.id")
    assert introduced_findings((f,), (f,)) == ()


def test_a_new_subject_is_introduced() -> None:
    base = (_structural("model.p.a", "orders.id"),)
    new = _structural("model.p.a", "payments.id")
    assert introduced_findings((*base, new), base) == (new,)


def test_drift_in_line_and_message_does_not_resurrect_a_finding() -> None:
    # The identity excludes the line span and message, the fields that drift between
    # two compilations, so the same issue moved and reworded reads as preexisting.
    # This is what separates the diff from naive whole-finding equality.
    head = (_structural("model.p.a", "orders.id", line=44, message="now phrased differently"),)
    base = (_structural("model.p.a", "orders.id", line=12, message="hazard"),)
    assert introduced_findings(head, base) == ()


def test_declaration_findings_diff_by_identity() -> None:
    base = (_declaration("model.p.a", column="amount", contract="Money"),)
    assert introduced_findings(base, base) == ()
    changed = _declaration("model.p.a", column="amount", contract="USD")
    assert introduced_findings((changed,), base) == (changed,)


def test_families_do_not_mask_each_other() -> None:
    # The family tag leads the identity tuple, so a base full of one family never
    # masks the other: a structural finding cannot be hidden by a declaration one.
    struct = _structural("model.p.a", "orders.id")
    decl = _declaration("model.p.a", column="amount", contract="Money")
    assert introduced_findings((struct,), (decl,)) == (struct,)


def test_a_fixed_finding_never_appears() -> None:
    # A finding in the base but not in HEAD (one the change fixed) is simply absent
    # from HEAD, so it never enters the introduced set: the diff reports introduced
    # findings, not fixed ones.
    base = (_structural("model.p.a", "orders.id"), _structural("model.p.b", "x.y"))
    head = (_structural("model.p.a", "orders.id"),)
    assert introduced_findings(head, base) == ()
