"""The fact bridge: registered contracts become substrate facts and findings.

This is the connective tissue between the authoring surface and the lineage
engine. It resolves every contract's ``dbt_model`` and column references against
the manifest, then emits the facts the propagation properties ground from:

* a :class:`~dblect.lineage.properties.domain_type.DomainTag` per typed
  magnitude column, with its unit and nominal companions bound to the columns
  they ride on (a fixed field pins a ``Concrete`` identity, an open one a
  ``PerRow`` binding);
* a candidate key per model carrying ``PrimaryKey`` markers, which merges in
  ``collect`` with the keys read from dbt ``unique`` tests and constraints;
* the foreign-key edges the grain analysis reads.

Resolution runs after the whole registry is populated, so a name that does not
resolve becomes a :class:`ContractIssue` and the remaining contracts still
ground their facts. The discoverers wrap this for the substrate's
``FactDiscoverer`` protocol. See ``docs/design/propagation-soundness.md``.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum, auto

from dblect.contracts import CapturedContract, ast
from dblect.lineage.facts.model import Declared, DeclaredSource, Fact
from dblect.lineage.graph import ColumnRef, SourceKind, SourceRef
from dblect.lineage.properties.domain_type import (
    NAKED,
    Concrete,
    Dimension,
    DomainTag,
    Nominal,
    PerRow,
    Unit,
    tagged,
)
from dblect.lineage.properties.functional_dependency import FD, FDSet
from dblect.lineage.properties.uniqueness import CandidateKeySet
from dblect.manifest import Manifest, Node, ResourceType
from dblect.manifest.parse import generic_test_target_uid
from dblect.types.contract import (
    Constraints,
    ContractRegistry,
    ContractSpec,
    DomainDecl,
    ForeignKeyDecl,
    PrimaryKeyDecl,
    active_registry,
)
from dblect.types.domain import DomainSpec
from dblect.types.scalars import FieldDef, FieldKind

# --- findings -------------------------------------------------------------------


class IssueCode(StrEnum):
    """The kinds of resolution failure a contract can surface."""

    UNRESOLVED_MODEL = auto()
    AMBIGUOUS_MODEL = auto()
    UNKNOWN_COLUMN = auto()
    UNSOURCED_FIELD = auto()
    OUT_OF_DOMAIN_VALUE = auto()
    MALFORMED_DECLARATION = auto()
    UNRESOLVED_FOREIGN_KEY = auto()


@dataclass(frozen=True, slots=True)
class ContractIssue:
    """One thing a contract declared that the manifest could not back up."""

    code: IssueCode
    contract: str
    message: str
    dbt_model: str | None = None
    field: str | None = None


# --- resolved outputs -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BoundTag:
    """A domain tag bound to the magnitude column it rides on."""

    column: ColumnRef
    tag: DomainTag


@dataclass(frozen=True, slots=True)
class ForeignKeyEdge:
    """A resolved foreign-key edge from a child column to a parent column."""

    child: ColumnRef
    parent: ColumnRef


@dataclass(frozen=True, slots=True)
class ColumnConstraint:
    """Checkable constraints attached to a resolved column."""

    column: ColumnRef
    constraints: Constraints


@dataclass(frozen=True, slots=True)
class ResolvedPredicate:
    """A ``@contract`` predicate collected for running. The analyzer never reasons
    over it; the execution loop compiles ``predicate`` to SQL and checks it against
    generated data for ``owner`` and any models the expression references."""

    contract: str
    owner: SourceRef
    predicate: ast.Pred


@dataclass(frozen=True, slots=True)
class ResolvedContracts:
    """Everything the bridge derived from the registry against one manifest."""

    tag_facts: tuple[Fact[DomainTag, ColumnRef], ...]
    key_facts: tuple[Fact[CandidateKeySet, SourceRef], ...]
    fd_facts: tuple[Fact[FDSet, SourceRef], ...]
    foreign_keys: tuple[ForeignKeyEdge, ...]
    constraints: tuple[ColumnConstraint, ...]
    predicates: tuple[ResolvedPredicate, ...]
    issues: tuple[ContractIssue, ...]


_KIND_OF_RESOURCE: Mapping[ResourceType, SourceKind] = {
    ResourceType.MODEL: SourceKind.MODEL,
    ResourceType.SEED: SourceKind.SEED,
    ResourceType.SNAPSHOT: SourceKind.SNAPSHOT,
    ResourceType.SOURCE: SourceKind.SOURCE,
}


def _source_of(node: Node) -> SourceRef:
    return SourceRef(_KIND_OF_RESOURCE.get(node.resource_type, SourceKind.MODEL), node.unique_id)


def _resolve_model(manifest: Manifest, ref: str) -> tuple[SourceRef | None, IssueCode | None]:
    """Resolve a ``dbt_model`` reference the way dbt resolves ``{{ ref() }}``:
    match the tail of a node's fqn, so a bare name matches by name and a dotted
    reference by its qualified suffix. More than one match is ambiguous."""
    parts = tuple(ref.split("."))
    candidates = [
        node
        for node in manifest.nodes.values()
        if node.is_data_flow and node.fqn[-len(parts) :] == parts
    ]
    if not candidates:
        return None, IssueCode.UNRESOLVED_MODEL
    if len(candidates) > 1:
        return None, IssueCode.AMBIGUOUS_MODEL
    return _source_of(candidates[0]), None


def _literal(value: object) -> str:
    """The case-insensitive identity a fixed value contributes to a tag."""
    return value.value if isinstance(value, StrEnum) else str(value)


def _column_of(spec: DomainSpec, fname: str) -> str:
    """The warehouse column an open field binds to: its explicit map, else its
    own name."""
    return spec.columns.get(fname, fname)


def _build_tag(
    decl_name: str, spec: DomainSpec, src: SourceRef, known: frozenset[str] | None
) -> tuple[BoundTag | None, list[ContractIssue]]:
    """Derive the bound tag for one domain-typed column, or the findings that
    keep it from grounding.

    Returns ``(None, [])`` when the type carries no magnitude (nothing to tag,
    and nothing wrong). Validation against a known column set runs only when one
    is supplied.
    """
    magnitudes = [f for f in spec.fields.values() if f.kind is FieldKind.MAGNITUDE]
    if not magnitudes:
        return None, []
    if len(magnitudes) > 1:
        return None, [
            ContractIssue(
                IssueCode.MALFORMED_DECLARATION,
                contract="",
                field=decl_name,
                message=(
                    f"declaration {decl_name!r} has {len(magnitudes)} magnitude fields; "
                    "a domain type carries at most one"
                ),
            )
        ]
    magnitude = magnitudes[0]

    out_of_domain = _out_of_domain_field(spec)
    if out_of_domain is not None:
        fname, value = out_of_domain
        return None, [
            ContractIssue(
                IssueCode.OUT_OF_DOMAIN_VALUE,
                contract="",
                field=decl_name,
                message=f"field {fname!r} fixed to {value!r}, which is not a valid value",
            )
        ]

    if known is not None:
        missing = _missing_column(spec, known)
        if missing is not None:
            fname, column, code = missing
            return None, [
                ContractIssue(
                    code,
                    contract="",
                    field=decl_name,
                    message=(f"field {fname!r} needs column {column!r}, absent from the model"),
                )
            ]

    dimension: Dimension | None = None
    for fdef in spec.fields.values():
        if fdef.kind is FieldKind.UNIT:
            unit = _unit_coordinate(fdef, spec, src)
            term = Dimension.of(unit)
            dimension = term if dimension is None else dimension.multiply(term)
    nominal: dict[str, Nominal] = {
        fdef.name: _nominal_coordinate(fdef, spec, src)
        for fdef in spec.fields.values()
        if fdef.kind is FieldKind.NOMINAL
    }
    tag = tagged(dimension=dimension, nominal=nominal)
    if tag == NAKED:
        return None, []
    return BoundTag(ColumnRef(src, _column_of(spec, magnitude.name)), tag), []


def _out_of_domain_field(spec: DomainSpec) -> tuple[str, object] | None:
    """The first enum field fixed to a value outside its enum, if any. A valid
    fixing is normalized to a member at authoring time, so a leftover raw string
    is exactly the out-of-domain case."""
    for fdef in spec.fields.values():
        if fdef.enum is None or fdef.name not in spec.fixed:
            continue
        value = spec.fixed[fdef.name]
        if not isinstance(value, fdef.enum):
            return fdef.name, value
    return None


def _missing_column(spec: DomainSpec, known: frozenset[str]) -> tuple[str, str, IssueCode] | None:
    """The first physical field whose backing column is absent from ``known``.

    A field is physical when it is neither fixed (logical) nor inert. An
    explicitly mapped column that is missing is an unknown column; an open field
    whose like-named column is missing is an unsourced field, the difference the
    finding names."""
    for fdef in spec.fields.values():
        if fdef.kind is FieldKind.INERT or fdef.name in spec.fixed:
            continue
        column = _column_of(spec, fdef.name)
        if column not in known:
            code = (
                IssueCode.UNKNOWN_COLUMN if fdef.name in spec.columns else IssueCode.UNSOURCED_FIELD
            )
            return fdef.name, column, code
    return None


def _unit_coordinate(fdef: FieldDef, spec: DomainSpec, src: SourceRef) -> Unit:
    if fdef.name in spec.fixed:
        return Concrete(_literal(spec.fixed[fdef.name]))
    return PerRow(ColumnRef(src, _column_of(spec, fdef.name)))


def _nominal_coordinate(fdef: FieldDef, spec: DomainSpec, src: SourceRef) -> Nominal:
    if fdef.name in spec.fixed:
        return Concrete(_literal(spec.fixed[fdef.name]))
    return PerRow(ColumnRef(src, _column_of(spec, fdef.name)))


def domain_tag(spec: DomainSpec, src: SourceRef) -> BoundTag | None:
    """The bound tag a well-formed domain spec contributes on ``src``, or ``None``.

    A thin public view of the binding rule: it returns the tag for a valid spec
    and ``None`` for one that carries no magnitude or an out-of-domain fixing.
    The bridge uses the richer :func:`_build_tag` to also surface why."""
    bound, _ = _build_tag("", spec, src, None)
    return bound


# --- resolution -----------------------------------------------------------------


def resolve_contracts(
    manifest: Manifest,
    *,
    known_columns: Mapping[SourceRef, frozenset[str]] | None = None,
    registry: ContractRegistry | None = None,
) -> ResolvedContracts:
    """Resolve every registered contract against ``manifest`` into facts and
    findings. ``known_columns`` enables column validation per relation; without
    it, column references are trusted (the manifest may not document columns)."""
    reg = registry if registry is not None else active_registry()
    out = _Accumulator()

    for cspec in reg.contracts:
        src, code = _resolve_model(manifest, cspec.dbt_model)
        if src is None:
            assert code is not None
            out.issues.append(
                ContractIssue(
                    code,
                    contract=cspec.name,
                    dbt_model=cspec.dbt_model,
                    message=f"dbt_model {cspec.dbt_model!r} did not resolve to one model",
                )
            )
            continue
        known = known_columns.get(src) if known_columns is not None else None
        _resolve_one(cspec, src, known, manifest, out)

    return ResolvedContracts(
        tag_facts=tuple(out.tag_facts),
        key_facts=tuple(out.key_facts),
        fd_facts=tuple(out.fd_facts),
        foreign_keys=tuple(out.foreign_keys),
        constraints=tuple(out.constraints),
        predicates=tuple(out.predicates),
        issues=tuple(out.issues),
    )


class _Accumulator:
    """The growing outputs of a resolution pass, one bucket per derived kind."""

    __slots__ = (
        "constraints",
        "fd_facts",
        "foreign_keys",
        "issues",
        "key_facts",
        "predicates",
        "tag_facts",
    )

    def __init__(self) -> None:
        self.tag_facts: list[Fact[DomainTag, ColumnRef]] = []
        self.key_facts: list[Fact[CandidateKeySet, SourceRef]] = []
        self.fd_facts: list[Fact[FDSet, SourceRef]] = []
        self.foreign_keys: list[ForeignKeyEdge] = []
        self.constraints: list[ColumnConstraint] = []
        self.predicates: list[ResolvedPredicate] = []
        self.issues: list[ContractIssue] = []


def _resolve_one(
    cspec: ContractSpec,
    src: SourceRef,
    known: frozenset[str] | None,
    manifest: Manifest,
    out: _Accumulator,
) -> None:
    key_columns: list[str] = []
    for fname, decl in cspec.declarations.items():
        form = decl.form
        if isinstance(form, DomainDecl):
            bound, found = _build_tag(fname, form.spec, src, known)
            out.issues.extend(replace(issue, contract=cspec.name) for issue in found)
            if bound is not None:
                out.tag_facts.append(
                    Fact(
                        scope=bound.column,
                        value=bound.tag,
                        provenance=Declared(DeclaredSource.USER_ASSERTED),
                        detail=f"{cspec.name}.{fname}",
                    )
                )
                # A constraint can only attach where we have a resolved column to
                # anchor it, and the bound magnitude column is the only ColumnRef
                # this bridge derives. Constraints on every other declaration form
                # below (scalar, key) and on a domain type that produced no tag
                # (the bound-is-None skip above) are dropped here: the
                # constraint-checking work that consumes ColumnConstraint will
                # resolve a column for those forms and carry their constraints.
                if decl.constraints is not None:
                    out.constraints.append(ColumnConstraint(bound.column, decl.constraints))
        elif isinstance(form, PrimaryKeyDecl):
            key_columns.append(fname)  # decl.constraints dropped (no anchor column yet)
        elif isinstance(form, ForeignKeyDecl):
            edge = _resolve_foreign_key(manifest, src, fname, form.target, cspec.name, out.issues)
            if edge is not None:
                out.foreign_keys.append(edge)  # decl.constraints dropped (no anchor column yet)
        # ScalarDecl carries no fact, and no ColumnConstraint, in this build.

    if key_columns:
        out.key_facts.append(
            Fact(
                scope=src,
                value=CandidateKeySet.of(frozenset(key_columns)),
                provenance=Declared(DeclaredSource.USER_ASSERTED),
                detail=cspec.name,
            )
        )

    _resolve_methods(cspec, src, known, manifest, out)


def _resolve_methods(
    cspec: ContractSpec,
    src: SourceRef,
    known: frozenset[str] | None,
    manifest: Manifest,
    out: _Accumulator,
) -> None:
    """Lower each captured ``@contract`` method onto the substrate. A fact becomes
    its matching ground fact (a dependency, a key, an edge); a predicate is set
    aside for the execution loop. A body that failed at capture is a finding."""
    for method in cspec.methods:
        if method.error is not None:
            out.issues.append(
                ContractIssue(
                    IssueCode.MALFORMED_DECLARATION,
                    contract=cspec.name,
                    field=method.name,
                    message=method.error,
                )
            )
            continue
        result = method.result
        assert result is not None  # a capture carries a result xor an error
        if isinstance(result, ast.Pred):
            out.predicates.append(ResolvedPredicate(cspec.name, src, result))
        else:
            _lower_fact(result, src, known, manifest, cspec.name, method, out)


def _lower_fact(
    fact: ast.FactNode,
    src: SourceRef,
    known: frozenset[str] | None,
    manifest: Manifest,
    contract: str,
    method: CapturedContract,
    out: _Accumulator,
) -> None:
    if isinstance(fact, ast.DeterminesFact):
        names = _own_columns(
            (*fact.determinant, fact.dependent), contract, method, out.issues, known
        )
        if names is None:
            return
        *determinant, dependent = names
        out.fd_facts.append(
            Fact(
                scope=src,
                value=FDSet.of(FD(frozenset(determinant), dependent)),
                provenance=Declared(DeclaredSource.USER_ASSERTED),
                detail=f"{contract}.{method.name}",
            )
        )
    elif isinstance(fact, (ast.KeyFact, ast.GrainFact)):
        columns = fact.columns if isinstance(fact, ast.KeyFact) else fact.per
        names = _own_columns(columns, contract, method, out.issues, known, fold=False)
        if names is None:
            return
        out.key_facts.append(
            Fact(
                scope=src,
                value=CandidateKeySet.of(frozenset(names)),
                provenance=Declared(DeclaredSource.USER_ASSERTED),
                detail=f"{contract}.{method.name}",
            )
        )
    else:
        _lower_references(fact, src, known, manifest, contract, method, out)


def _lower_references(
    fact: ast.ReferencesFact,
    src: SourceRef,
    known: frozenset[str] | None,
    manifest: Manifest,
    contract: str,
    method: CapturedContract,
    out: _Accumulator,
) -> None:
    if fact.child.model is not None:
        out.issues.append(
            ContractIssue(
                IssueCode.MALFORMED_DECLARATION,
                contract=contract,
                field=method.name,
                message="a references edge starts on the contract's own column (self), not models.*",
            )
        )
        return
    if known is not None and fact.child.name not in known:
        out.issues.append(
            ContractIssue(
                IssueCode.UNKNOWN_COLUMN,
                contract=contract,
                field=method.name,
                message=f"column {fact.child.name!r} is absent from the model",
            )
        )
        return
    if fact.parent.model is None:
        out.issues.append(
            ContractIssue(
                IssueCode.UNRESOLVED_FOREIGN_KEY,
                contract=contract,
                field=method.name,
                message="references(...) needs a target on another model (models.parent.column)",
            )
        )
        return
    parent_src = _resolve_model(manifest, fact.parent.model)[0]
    if parent_src is None:
        out.issues.append(
            ContractIssue(
                IssueCode.UNRESOLVED_FOREIGN_KEY,
                contract=contract,
                field=method.name,
                message=f"references target model {fact.parent.model!r} did not resolve",
            )
        )
        return
    out.foreign_keys.append(
        ForeignKeyEdge(
            child=ColumnRef(src, fact.child.name),
            parent=ColumnRef(parent_src, fact.parent.name),
        )
    )


def _own_columns(
    columns: tuple[ast.Col, ...],
    contract: str,
    method: CapturedContract,
    issues: list[ContractIssue],
    known: frozenset[str] | None,
    *,
    fold: bool = True,
) -> list[str] | None:
    """The column names of ``columns``, all of which must live on the contract's
    own model and (when ``known`` is supplied) name a real column. A reference into
    another model is a finding, since the dependency and key facts the substrate
    carries are single-relation; a column absent from ``known`` is an unknown-column
    finding, matching the declaration path. ``fold`` case-folds the returned names
    to the dependency property's lowercased column universe; key and grain facts
    keep the authored case, which is why they pass ``fold=False``."""
    names: list[str] = []
    for col in columns:
        if col.model is not None:
            issues.append(
                ContractIssue(
                    IssueCode.MALFORMED_DECLARATION,
                    contract=contract,
                    field=method.name,
                    message=(
                        f"column {col.model}.{col.name} is on another model; this fact "
                        "ranges over the contract's own relation"
                    ),
                )
            )
            return None
        if known is not None and col.name not in known:
            issues.append(
                ContractIssue(
                    IssueCode.UNKNOWN_COLUMN,
                    contract=contract,
                    field=method.name,
                    message=f"column {col.name!r} is absent from the model",
                )
            )
            return None
        names.append(col.name.lower() if fold else col.name)
    return names


def _resolve_foreign_key(
    manifest: Manifest,
    child_src: SourceRef,
    fname: str,
    target: str,
    contract: str,
    issues: list[ContractIssue],
) -> ForeignKeyEdge | None:
    model_ref, _, column = target.rpartition(".")
    parent_src = _resolve_model(manifest, model_ref)[0] if model_ref else None
    if parent_src is None or not column:
        issues.append(
            ContractIssue(
                IssueCode.UNRESOLVED_FOREIGN_KEY,
                contract=contract,
                field=fname,
                message=f"foreign key target {target!r} did not resolve",
            )
        )
        return None
    return ForeignKeyEdge(
        child=ColumnRef(child_src, fname),
        parent=ColumnRef(parent_src, column),
    )


# --- foreign keys from dbt relationships tests ----------------------------------


def dbt_relationship_edges(manifest: Manifest) -> tuple[ForeignKeyEdge, ...]:
    """The foreign-key edges a project's dbt ``relationships`` tests already
    state, read the way a ``unique`` test is read as a key.

    The test is attached to the child model and carries the child column
    (``column_name``) and parent column (``field``); the parent relation is the
    other data-flow node the test depends on. A test whose parent cannot be
    pinned that way is skipped rather than guessed.
    """
    edges: list[ForeignKeyEdge] = []
    for node in manifest.nodes.values():
        tm = node.test_metadata
        if tm is None or not tm.enabled or tm.name != "relationships":
            continue
        child_col = tm.kwargs.get("column_name")
        parent_col = tm.kwargs.get("field")
        if not isinstance(child_col, str) or not child_col:
            continue
        if not isinstance(parent_col, str) or not parent_col:
            continue
        child_uid = generic_test_target_uid(node)
        if child_uid is None or child_uid not in manifest.nodes:
            continue
        parent_uid = _relationship_parent(manifest, node, child_uid, tm.kwargs.get("to"))
        if parent_uid is None:
            continue
        edges.append(
            ForeignKeyEdge(
                child=ColumnRef(_source_of(manifest.nodes[child_uid]), child_col),
                parent=ColumnRef(_source_of(manifest.nodes[parent_uid]), parent_col),
            )
        )
    return tuple(edges)


def _relationship_parent(manifest: Manifest, node: Node, child_uid: str, to: object) -> str | None:
    """The parent relation a relationships test points at: the one other
    data-flow node it depends on, or (if that is ambiguous) the node the ``to``
    ref names."""
    candidates = [
        dep
        for dep in node.depends_on
        if dep != child_uid and dep in manifest.nodes and manifest.nodes[dep].is_data_flow
    ]
    if len(candidates) == 1:
        return candidates[0]
    if isinstance(to, str):
        name = _ref_name(to)
        if name is not None:
            named = [uid for uid in candidates if manifest.nodes[uid].name == name]
            if len(named) == 1:
                return named[0]
    return None


def _ref_name(to: str) -> str | None:
    """The model name inside a ``ref('...')`` / ``source('...', 'name')`` string,
    or ``None`` if it does not parse. The last quoted token is the relation name."""
    quoted = re.findall(r"'([^']+)'", to)
    return quoted[-1] if quoted else None


def foreign_key_edges(
    manifest: Manifest, *, registry: ContractRegistry | None = None
) -> tuple[ForeignKeyEdge, ...]:
    """Every foreign-key edge the project declares: contract ``ForeignKey``
    markers merged with dbt ``relationships`` tests, de-duplicated so an edge
    stated both ways appears once. The merge point a future fan-out finding or
    fixture generator reads from."""
    contract_edges = resolve_contracts(manifest, registry=registry).foreign_keys
    return tuple(dict.fromkeys((*contract_edges, *dbt_relationship_edges(manifest))))


# --- discoverers ----------------------------------------------------------------


class _TagDiscoverer:
    """Yields the contract-sourced domain-type facts. Findings are dropped here;
    they surface through :func:`resolve_contracts` for the report."""

    def discover(
        self, manifest: Manifest, *, name_to_source: Mapping[str, SourceRef]
    ) -> Collection[Fact[DomainTag, ColumnRef]]:
        return resolve_contracts(manifest).tag_facts


class _KeyDiscoverer:
    """Yields the contract-sourced candidate-key facts (from ``PrimaryKey``
    markers and ``self.key`` / ``self.grain`` methods), to be merged with the
    dbt-test-sourced keys in ``collect``."""

    def discover(
        self, manifest: Manifest, *, name_to_source: Mapping[str, SourceRef]
    ) -> Collection[Fact[CandidateKeySet, SourceRef]]:
        return resolve_contracts(manifest).key_facts


class _FdDiscoverer:
    """Yields the contract-sourced functional-dependency facts (from
    ``self.a.determines(self.b)`` methods) that ground the FD property."""

    def discover(
        self, manifest: Manifest, *, name_to_source: Mapping[str, SourceRef]
    ) -> Collection[Fact[FDSet, SourceRef]]:
        return resolve_contracts(manifest).fd_facts


def contract_tag_discoverer() -> _TagDiscoverer:
    return _TagDiscoverer()


def contract_key_discoverer() -> _KeyDiscoverer:
    return _KeyDiscoverer()


def contract_fd_discoverer() -> _FdDiscoverer:
    return _FdDiscoverer()
