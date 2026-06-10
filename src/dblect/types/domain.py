"""``DomainType``: a class read as a schema, refined and bound but never built.

A ``DomainType`` subclass declares fields with ordinary annotations, and the
metaclass reads them into a :class:`DomainSpec`: the field definitions, the
values fixed on them, and the columns they bind to. The three ways the author
narrows a type all land in the same spec, so they mean the same thing:

* ``T.refine(field=value)`` names a reusable refined type;
* ``T.columns(field="col")`` maps fields to warehouse columns;
* the call form ``T(field=value, amount="col")`` is sugar for both, splitting a
  magnitude's string into a column map and everything else into a fixing;
* subclassing extends (adds fields) and fixes (a class-level default), with
  multiple inheritance taking the union of facets and requiring agreement where
  two bases fix the same field.

This mirrors Pydantic's class-as-declaration shape without its runtime: the
class is never instantiated to validate a row. Calling it returns a *narrowed
type*, not an instance. See ``docs/design/declaration-dsl.md``.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Self, cast

from dblect.types.errors import DomainTypeError
from dblect.types.scalars import FieldDef, FieldKind, classify

_FIXED_OVERRIDES = "__dblect_fixed_overrides__"
_COLUMN_OVERRIDES = "__dblect_column_overrides__"


@dataclass(frozen=True)
class DomainSpec:
    """The resolved schema of a domain type: fields, fixings, column bindings.

    Equality is structural, so every spelling route to the same narrowed type
    compares equal regardless of how it was built.
    """

    fields: Mapping[str, FieldDef]
    fixed: Mapping[str, object]
    columns: Mapping[str, str]


def _coerce_fixing(fdef: FieldDef, value: object) -> object:
    """Validate and normalize a value fixed onto a field.

    A magnitude cannot be fixed to a literal. An enum field accepts a member of
    its own enum, or a string that names a member (round-tripped to the member
    so the two spellings unify); a string that names no member is kept verbatim,
    so the out-of-domain case surfaces as a finding at resolution rather than an
    authoring crash. A member of a *different* enum, or a wrong Python type, is
    an authoring error.
    """
    if fdef.kind is FieldKind.MAGNITUDE:
        raise DomainTypeError(
            f"field {fdef.name!r} is a magnitude and cannot be fixed to a literal"
        )
    if fdef.enum is not None:
        if isinstance(value, fdef.enum):
            return value
        if isinstance(value, StrEnum):
            raise DomainTypeError(
                f"field {fdef.name!r} expects {fdef.enum.__name__}, got member of "
                f"{type(value).__name__}"
            )
        if isinstance(value, str):
            try:
                return fdef.enum(value)
            except ValueError:
                return value  # out of domain: kept for the finding, not raised
        raise DomainTypeError(f"field {fdef.name!r} expects {fdef.enum.__name__}, got {value!r}")
    if fdef.pytype is bool:
        if not isinstance(value, bool):
            raise DomainTypeError(f"field {fdef.name!r} expects a bool, got {value!r}")
        return value
    if fdef.pytype is str:
        if not isinstance(value, str):
            raise DomainTypeError(f"field {fdef.name!r} expects a str, got {value!r}")
        return value
    raise DomainTypeError(f"field {fdef.name!r} ({fdef.kind}) cannot be fixed")


class DomainTypeMeta(type):
    """Reads a ``DomainType`` body into a :class:`DomainSpec` and turns the call
    form into a narrowing.

    ``refine`` / ``columns`` / ``spec`` are ordinary classmethods on
    :class:`DomainType` so a reader and a type checker both see them on the class;
    the metaclass owns only what must live here: the spec computation at class
    creation and the call-form override of ``Type(...)``.
    """

    __dblect_spec__: DomainSpec

    def __new__(
        mcls, name: str, bases: tuple[type, ...], namespace: dict[str, object]
    ) -> DomainTypeMeta:
        cls = super().__new__(mcls, name, bases, namespace)
        cls.__dblect_spec__ = _build_spec(cls, bases, namespace)
        return cls

    def __call__(cls, **kwargs: object) -> type[DomainType]:
        """Call form: a magnitude field's string is a column mapping, every other
        keyword is a fixing. Equivalent to ``.columns(...).refine(...)``."""
        return cast("type[DomainType]", _call_form(cls, kwargs))


def _spec_of(cls: DomainTypeMeta) -> DomainSpec:
    return cls.__dblect_spec__


def _build_spec(
    cls: DomainTypeMeta, bases: tuple[type, ...], namespace: Mapping[str, object]
) -> DomainSpec:
    fields: dict[str, FieldDef] = {}
    columns: dict[str, str] = {}
    # base fixings tracked per field so a disagreement between two bases is a
    # conflict only when nothing downstream settles it.
    base_fixed: dict[str, set[object]] = {}

    for base in bases:
        spec = cast("DomainSpec | None", getattr(base, "__dblect_spec__", None))
        if spec is None:
            continue
        for fname, fdef in spec.fields.items():
            existing = fields.get(fname)
            if existing is not None and existing != fdef:
                raise DomainTypeError(
                    f"field {fname!r} is declared with conflicting types across bases"
                )
            fields[fname] = fdef
        columns.update(spec.columns)
        for fname, value in spec.fixed.items():
            base_fixed.setdefault(fname, set()).add(value)

    own_annotations = inspect.get_annotations(cls, eval_str=True)
    for fname, annotation in own_annotations.items():
        if fname.startswith("_"):
            continue
        fdef = classify(fname, annotation)
        existing = fields.get(fname)
        if existing is not None and existing != fdef:
            raise DomainTypeError(
                f"field {fname!r} redeclares an inherited field with a different type"
            )
        fields[fname] = fdef

    fixed: dict[str, object] = {}
    unresolved: dict[str, set[object]] = {}
    for fname, values in base_fixed.items():
        if len(values) == 1:
            fixed[fname] = next(iter(values))
        else:
            unresolved[fname] = values

    # Class-level defaults on annotated fields are fixings (the class-level twin
    # of refine), and they settle any base disagreement.
    for fname in own_annotations:
        if fname.startswith("_") or fname not in namespace:
            continue
        fixed[fname] = _coerce_fixing(fields[fname], namespace[fname])
        unresolved.pop(fname, None)

    # Synthetic refine/columns overrides, already validated, win last.
    raw_columns = namespace.get(_COLUMN_OVERRIDES)
    if isinstance(raw_columns, Mapping):
        columns.update(cast("Mapping[str, str]", raw_columns))
    raw_fixed = namespace.get(_FIXED_OVERRIDES)
    if isinstance(raw_fixed, Mapping):
        for k, v in cast("Mapping[str, object]", raw_fixed).items():
            fixed[k] = v
            unresolved.pop(k, None)

    if unresolved:
        culprit = ", ".join(sorted(unresolved))
        raise DomainTypeError(
            f"field(s) {culprit} fixed to disagreeing values across bases; "
            "override in the subclass to settle"
        )

    return DomainSpec(fields=fields, fixed=fixed, columns=columns)


def _specialize(
    base: DomainTypeMeta,
    *,
    fixed_overrides: Mapping[str, object] | None = None,
    column_overrides: Mapping[str, str] | None = None,
) -> DomainTypeMeta:
    """Create a narrowed subclass carrying the (already validated) overrides."""
    namespace: dict[str, object] = {}
    if fixed_overrides:
        namespace[_FIXED_OVERRIDES] = dict(fixed_overrides)
    if column_overrides:
        namespace[_COLUMN_OVERRIDES] = dict(column_overrides)
    return DomainTypeMeta(base.__name__, (base,), namespace)


def _refine(cls: DomainTypeMeta, fixings: Mapping[str, object]) -> DomainTypeMeta:
    spec = _spec_of(cls)
    overrides: dict[str, object] = {}
    for fname, value in fixings.items():
        fdef = spec.fields.get(fname)
        if fdef is None:
            raise DomainTypeError(f"refine: unknown field {fname!r}")
        overrides[fname] = _coerce_fixing(fdef, value)
    return _specialize(cls, fixed_overrides=overrides)


def _columns(cls: DomainTypeMeta, mapping: Mapping[str, object]) -> DomainTypeMeta:
    spec = _spec_of(cls)
    bindings: dict[str, str] = {}
    for fname, column in mapping.items():
        if fname not in spec.fields:
            raise DomainTypeError(f"columns: unknown field {fname!r}")
        if not isinstance(column, str):
            raise DomainTypeError(f"columns: {fname!r} must map to a column name, got {column!r}")
        bindings[fname] = column
    return _specialize(cls, column_overrides=bindings)


def _call_form(cls: DomainTypeMeta, kwargs: Mapping[str, object]) -> DomainTypeMeta:
    spec = _spec_of(cls)
    column_maps: dict[str, str] = {}
    fixings: dict[str, object] = {}
    for fname, value in kwargs.items():
        fdef = spec.fields.get(fname)
        if fdef is None:
            raise DomainTypeError(f"unknown field {fname!r}")
        if fdef.kind is FieldKind.MAGNITUDE:
            if not isinstance(value, str):
                raise DomainTypeError(
                    f"field {fname!r} is a magnitude; pass a column name, not {value!r}"
                )
            column_maps[fname] = value
        else:
            fixings[fname] = value
    result = cls
    if column_maps:
        result = _columns(result, column_maps)
    if fixings:
        result = _refine(result, fixings)
    if result is cls:
        result = _specialize(cls)
    return result


class DomainType(metaclass=DomainTypeMeta):
    """Base class for domain types. Subclass it, declare fields, and narrow with
    ``refine`` / ``columns`` / the call form. Never instantiated to a value."""

    @classmethod
    def spec(cls) -> DomainSpec:
        """The resolved schema of this (possibly narrowed) type."""
        return _spec_of(cls)

    @classmethod
    def refine(cls, **fixings: object) -> type[Self]:
        """A narrowed type with the named fields fixed to values."""
        return cast("type[Self]", _refine(cls, fixings))

    @classmethod
    def columns(cls, **mapping: object) -> type[Self]:
        """A narrowed type with the named fields bound to warehouse columns."""
        return cast("type[Self]", _columns(cls, mapping))
