"""The findings ``dblect check`` reports, and the report that carries them.

These are declaration-level findings: a contract that does not line up with the
manifest, a declared domain type contradicted by what the substrate propagates, a
sum the algebra cannot call well typed. They are distinct from the SQL-structural
findings the ``audit`` walker emits, so they carry their own kinds and their own
small report shape rather than borrowing the SQL ``Finding`` (which is a span in
one statement). See ``docs/design/declaration-dsl.md`` and
``docs/design/propagation-soundness.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, auto

from dblect.loader import LoadIssue


class CheckFindingKind(StrEnum):
    """What a check finding is about."""

    CONTRACT_ISSUE = auto()
    """A contract did not resolve against the manifest (unknown model, unsourced
    field, out-of-domain value, malformed declaration)."""

    DOMAIN_TYPE_CONTRADICTION = auto()
    """A declared domain type is contradicted by the type the substrate inferred
    for the same column, and the contradiction rides the DAG (currency creep)."""

    AGGREGATION_NOT_WELL_TYPED = auto()
    """A reduction over one field of a multi-field type whose other fields are not
    provably constant across the group (the mixed-currency sum)."""


@dataclass(frozen=True, slots=True)
class CheckFinding:
    """One declaration-level finding, located on the model it lands on."""

    kind: CheckFindingKind
    message: str
    model_unique_id: str | None
    file_path: str | None = None
    column: str | None = None
    contract: str | None = None


@dataclass(frozen=True, slots=True)
class UnbuiltModel:
    """A model dblect could not analyze (no compiled SQL, or a parse/qualify
    failure), with the reason. Surfaced so a model the analysis could not read is
    never mistaken for one it read and found clean."""

    unique_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class CheckReport:
    """The output of one ``run_check``: findings, the modules that failed to load,
    the models that could not be analyzed, and a few counts for the summary line."""

    findings: tuple[CheckFinding, ...]
    load_issues: tuple[LoadIssue, ...]
    unbuilt: tuple[UnbuiltModel, ...]
    contracts_resolved: int
    models_propagated: int
    predicates_collected: int

    @property
    def has_findings(self) -> bool:
        return bool(self.findings) or bool(self.load_issues)

    @property
    def models_analyzed(self) -> int:
        return self.models_propagated - len(self.unbuilt)
