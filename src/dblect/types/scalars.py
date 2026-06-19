"""Scalar field types and the rule that classifies a field by its meaning.

A domain type's field is read by what it *is*, not annotated by hand. The
classification is the seam between the author's ordinary Python annotations and
the substrate's tag algebra (``docs/design/domain-type-algebra.md``):

* a numeric :class:`Decimal` (or ``Decimal(p, s)``), a :class:`Count`, or a
  floating-point ``float`` / :class:`Float` is a **magnitude**, the field a tag
  rides on;
* a :class:`~dblect.types.enums.UnitEnum` (a currency) is a **unit**, the
  dimensional companion of the magnitude;
* a :class:`~dblect.types.enums.NominalEnum`, a ``bool``, a ``str``, or a
  :class:`Varchar` is a **nominal** category, carried by equality;
* a :class:`Date`, a :class:`Timestamp` / ``datetime``, and a bare integer
  (``int`` / :class:`Integer` / :class:`BigInt`) carry **no** tag.

A bare integer is the one scalar whose role its algebra does not settle: an
integer is algebraically a perfect quantity, yet by role it is as often an
identifier or a calendar year, which are tags. The lenient default reads it as
opaque (inert), making no claim either way; a measure is spelled ``Count`` /
``Decimal`` and an identifier or year carries its own domain type. A future
strict mode rejects a bare integer instead, per the lenient/strict switch in
``docs/design/domain-type-algebra.md``.

Each field becomes one :class:`FieldDef`. The kinds are what
``docs/design/declaration-dsl.md`` calls the magnitude/tag classification
"falling out of the types' algebra" rather than being declared.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum, auto

from dblect.types.enums import NominalEnum, UnitEnum
from dblect.types.errors import DomainTypeError


class FieldKind(StrEnum):
    """How a field participates in a tag."""

    MAGNITUDE = auto()  # the numeric value a tag rides on (an amount, a count)
    UNIT = auto()  # a dimensional companion (a currency)
    NOMINAL = auto()  # a categorical companion carried by equality
    INERT = auto()  # a scalar that carries no tag (a date)


@dataclass(frozen=True, slots=True)
class FieldDef:
    """One field of a domain type, classified.

    ``enum`` is set for a unit or nominal enum field; ``pytype`` is set for a
    plain ``bool``/``str`` nominal field; ``precision``/``scale`` carry a
    decimal magnitude's parameters. They pin what a fixing must look like and
    how the bridge builds the field's tag coordinate.
    """

    name: str
    kind: FieldKind
    enum: type[StrEnum] | None = None
    pytype: type | None = None
    precision: int | None = None
    scale: int | None = None


class Decimal:
    """A fixed-point magnitude type, usable bare (``Decimal``) or parameterized
    (``Decimal(18, 2)``). The parameters annotate precision and scale; the
    substrate does not yet reason over them, but the contract carries them so a
    later width check has them to hand."""

    __slots__ = ("precision", "scale")

    def __init__(self, precision: int | None = None, scale: int | None = None) -> None:
        self.precision = precision
        self.scale = scale


class Count:
    """A dimensionless magnitude: a row or item count."""


class Float:
    """A floating-point magnitude type. Unlike a bare integer, a float has no
    identifier or calendar-year role to confuse it with, so it is a magnitude."""


class Integer:
    """A bare integer column. Inert under the lenient default: an integer is
    algebraically a quantity yet by role often an identifier or a year, so it
    makes no domain claim. Spell a measure ``Count`` / ``Decimal`` and an
    identifier or year with its domain type."""


class BigInt:
    """A 64-bit integer column. Inert, the wide sibling of :class:`Integer`."""


class Date:
    """A calendar date. Inert: it carries no domain tag of its own."""


class Timestamp:
    """A date-time. Inert, the timestamp sibling of :class:`Date`."""


class Varchar:
    """A variable-length string column, treated as a nominal category."""


def classify(name: str, annotation: object) -> FieldDef:
    """Read one field annotation into a :class:`FieldDef`, or raise.

    The order matters only where the Python types overlap (``bool`` is an
    ``int``; an enum member is a ``str``); each branch tests an exact identity
    or a marker base, so the overlap never misroutes.
    """
    if annotation is Decimal:
        return FieldDef(name, FieldKind.MAGNITUDE)
    if isinstance(annotation, Decimal):
        return FieldDef(
            name, FieldKind.MAGNITUDE, precision=annotation.precision, scale=annotation.scale
        )
    if annotation is Count:
        return FieldDef(name, FieldKind.MAGNITUDE)
    if annotation is float or annotation is Float:
        return FieldDef(name, FieldKind.MAGNITUDE)
    if isinstance(annotation, type) and issubclass(annotation, UnitEnum):
        return FieldDef(name, FieldKind.UNIT, enum=annotation)
    if isinstance(annotation, type) and issubclass(annotation, NominalEnum):
        return FieldDef(name, FieldKind.NOMINAL, enum=annotation)
    if annotation is bool:
        return FieldDef(name, FieldKind.NOMINAL, pytype=bool)
    if annotation is str or annotation is Varchar:
        return FieldDef(name, FieldKind.NOMINAL, pytype=str)
    if annotation is Date or annotation is Timestamp or annotation is datetime:
        return FieldDef(name, FieldKind.INERT)
    # Lenient default: a bare integer makes no domain claim, so it is inert. A
    # future strict mode rejects it here and teaches Count/Decimal or a domain
    # type; see the lenient/strict switch in domain-type-algebra.md.
    if annotation is int or annotation is Integer or annotation is BigInt:
        return FieldDef(name, FieldKind.INERT)
    raise DomainTypeError(
        f"field {name!r}: {annotation!r} is not a domain field type "
        "(use Decimal/Count/Float, a UnitEnum or NominalEnum subclass, bool, str, "
        "int, Date, or Timestamp)"
    )
