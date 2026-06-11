"""``ModelContract``: binding domain types to one dbt model's columns.

Defining a contract class *is* the declaration. ``__init_subclass__`` reads the
class body into a :class:`ContractSpec` and registers it, the import-time
discovery Pydantic and pytest use. A class without its own ``dbt_model`` is an
abstract base whose field declarations flow to concrete subclasses but which is
not itself a contract on any model.

Each annotated field becomes one declaration: a domain type (the column carries
that meaning), a ``PrimaryKey`` / ``ForeignKey`` marker (a key the grain
analysis reads), or a scalar type. ``Field(...)`` carries the Pydantic-style
constraint vocabulary and inline fixings at one binding site. Resolution against
the manifest happens later in the bridge, so a misspelled model is a finding,
not an ``ImportError`` that blinds the audit. See
``docs/design/declaration-dsl.md``.
"""

from __future__ import annotations

import inspect
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass

from dblect.types.domain import DomainSpec, DomainType, DomainTypeMeta
from dblect.types.errors import DomainTypeError
from dblect.types.scalars import FieldDef, classify

# --- field constraints (the Pydantic-style vocabulary) -------------------------


@dataclass(frozen=True, slots=True)
class Constraints:
    """Checkable bounds on a column's own values, the slice of Pydantic's
    ``Field`` vocabulary dblect can prove or refute against data."""

    ge: float | None = None
    gt: float | None = None
    le: float | None = None
    lt: float | None = None
    multiple_of: float | None = None
    min_length: int | None = None
    max_length: int | None = None


_CONSTRAINT_KEYS = frozenset({"ge", "gt", "le", "lt", "multiple_of", "min_length", "max_length"})
# Readable aliases that expand to a constraint when truthy.
_CONSTRAINT_ALIASES: Mapping[str, tuple[str, float]] = {
    "non_negative": ("ge", 0),
    "positive": ("gt", 0),
}


@dataclass(frozen=True, slots=True)
class _FieldSpec:
    """What a ``Field(...)`` call captured: checkable constraints and inline
    fixings (the vouched-meaning half, applied as a refinement of the column's
    declared type)."""

    constraints: Constraints | None
    fixings: Mapping[str, object]


def Field(**kwargs: object) -> _FieldSpec:  # noqa: N802 (Pydantic-style constructor name)
    """Column-level metadata: checkable constraints (``ge``/``gt``/...) and
    inline fixings (``contains_tax=False``). Constraints are proved against data;
    fixings narrow the column's declared domain type."""
    bounds: dict[str, float] = {}
    fixings: dict[str, object] = {}
    for key, value in kwargs.items():
        if key in _CONSTRAINT_ALIASES:
            target, fixed_value = _CONSTRAINT_ALIASES[key]
            if value:
                bounds[target] = fixed_value
        elif key in _CONSTRAINT_KEYS:
            if not isinstance(value, (int, float)):
                raise DomainTypeError(f"Field constraint {key!r} expects a number, got {value!r}")
            bounds[key] = float(value)
        else:
            fixings[key] = value
    return _FieldSpec(
        constraints=_constraints_from(bounds) if bounds else None,
        fixings=fixings,
    )


def _constraints_from(bounds: Mapping[str, float]) -> Constraints:
    """Assemble a :class:`Constraints` from a flat bounds map, keeping the
    length bounds integral and the numeric bounds real."""

    def num(key: str) -> float | None:
        return bounds.get(key)

    def length(key: str) -> int | None:
        value = bounds.get(key)
        return int(value) if value is not None else None

    return Constraints(
        ge=num("ge"),
        gt=num("gt"),
        le=num("le"),
        lt=num("lt"),
        multiple_of=num("multiple_of"),
        min_length=length("min_length"),
        max_length=length("max_length"),
    )


# --- key markers ----------------------------------------------------------------


class PrimaryKey:
    """Marker annotation: this column is (part of) the model's primary key."""


class ForeignKey:
    """Marker annotation naming another model's column: ``ForeignKey("dim.col")``.

    Doubles as the grain edge the fan-out analysis reads. An existing dbt
    ``relationships`` test is read as the same fact, so a project need not
    restate it.
    """

    __slots__ = ("target",)

    def __init__(self, target: str) -> None:
        self.target = target


# --- declaration forms ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DomainDecl:
    """A column typed with a domain type."""

    spec: DomainSpec


@dataclass(frozen=True, slots=True)
class ScalarDecl:
    """A column typed with a bare scalar or enum, carrying no companion structure."""

    type: FieldDef


@dataclass(frozen=True, slots=True)
class PrimaryKeyDecl:
    """A column declared as part of the primary key."""


@dataclass(frozen=True, slots=True)
class ForeignKeyDecl:
    """A column declared as a foreign key to ``target`` (``"model.column"``)."""

    target: str


DeclForm = DomainDecl | ScalarDecl | PrimaryKeyDecl | ForeignKeyDecl


@dataclass(frozen=True, slots=True)
class ContractField:
    """One column declaration plus any ``Field`` constraints attached to it."""

    form: DeclForm
    constraints: Constraints | None = None


@dataclass(frozen=True, slots=True)
class ContractSpec:
    """A model contract read into data: which model, and what each column means."""

    name: str
    dbt_model: str
    declarations: Mapping[str, ContractField]


# --- the registry ---------------------------------------------------------------


class ContractRegistry:
    """Where defined contracts collect. The bridge reads it after the whole
    project scan, so resolution sees every contract at once."""

    def __init__(self) -> None:
        self._contracts: list[ContractSpec] = []

    @property
    def contracts(self) -> tuple[ContractSpec, ...]:
        return tuple(self._contracts)

    def register(self, spec: ContractSpec) -> None:
        self._contracts.append(spec)


_active_registry = ContractRegistry()


def active_registry() -> ContractRegistry:
    """The registry contracts currently register into."""
    return _active_registry


@contextmanager
def isolated_registry() -> Generator[ContractRegistry, None, None]:
    """Swap in a fresh registry for the duration of the block, restoring the
    previous one after. Keeps a test's contract definitions from leaking into
    another's, and gives a future ``dblect.scan`` a clean scope to populate."""
    global _active_registry
    previous = _active_registry
    _active_registry = ContractRegistry()
    try:
        yield _active_registry
    finally:
        _active_registry = previous


# --- reading a contract body ----------------------------------------------------

_RESERVED = frozenset({"dbt_model"})


def _build_declaration(name: str, annotation: object, default: object) -> ContractField:
    field_spec = default if isinstance(default, _FieldSpec) else None
    fixings: Mapping[str, object] = field_spec.fixings if field_spec is not None else {}

    form: DeclForm
    if isinstance(annotation, DomainTypeMeta) and issubclass(annotation, DomainType):
        domain_type = annotation
        if fixings:
            domain_type = domain_type.refine(**fixings)
        form = DomainDecl(domain_type.spec())
    elif annotation is PrimaryKey:
        if fixings:
            raise DomainTypeError(f"{name!r}: a primary key cannot carry inline fixings")
        form = PrimaryKeyDecl()
    elif isinstance(annotation, ForeignKey):
        if fixings:
            raise DomainTypeError(f"{name!r}: a foreign key cannot carry inline fixings")
        form = ForeignKeyDecl(annotation.target)
    else:
        fdef = classify(name, annotation)
        if fixings:
            raise DomainTypeError(f"{name!r}: inline fixings need a domain type, not a bare scalar")
        form = ScalarDecl(fdef)

    constraints = field_spec.constraints if field_spec is not None else None
    return ContractField(form=form, constraints=constraints)


def _collect_declarations(cls: type) -> dict[str, ContractField]:
    """Merge field declarations across the MRO so an abstract base's columns flow
    to its concrete subclasses; a subclass's own declaration wins."""
    declarations: dict[str, ContractField] = {}
    for klass in reversed(cls.__mro__):
        if klass is object or klass is ModelContract:
            continue
        annotations = inspect.get_annotations(klass, eval_str=True)
        for name, annotation in annotations.items():
            if name in _RESERVED or name.startswith("__"):
                continue
            default = klass.__dict__.get(name, _MISSING)
            declarations[name] = _build_declaration(name, annotation, default)
    return declarations


_MISSING = object()


class ModelContract:
    """Base class for model contracts. Subclass it, set ``dbt_model``, and declare
    one column per field. A subclass without its own ``dbt_model`` is an abstract
    base; its declarations flow to concrete subclasses."""

    dbt_model: str

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        own_model = cls.__dict__.get("dbt_model")
        declarations = _collect_declarations(cls)
        if not isinstance(own_model, str):
            return  # abstract base: declarations flow down, nothing registered
        spec = ContractSpec(
            name=cls.__qualname__,
            dbt_model=own_model,
            declarations=declarations,
        )
        active_registry().register(spec)
