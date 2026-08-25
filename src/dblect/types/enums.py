"""Marker base classes that tell dblect how to treat a domain type's enum
fields: as a unit of measure, or as a plain category.

A :class:`UnitEnum` is a unit, like a currency: multiplying or dividing two
magnitudes combines or cancels their units the way exponents do (dividing two
dollar amounts by each other cancels the currency). A :class:`NominalEnum` is a
plain category, like a country code, compared only for equality. Both subclass
:class:`enum.StrEnum`, so a member equals its string code and ``MyUnit("USD")``
round-trips a literal, which is what lets a contract accept ``currency="USD"``
and ``currency=MyUnit.USD`` alike (an out-of-domain literal is a finding,
raised by neither the enum nor here).

A project declares its own vocabularies by subclassing these. The
``dblect.demo`` package ships partial ISO 4217 / 3166-1 slices to drive the
walkthrough.
"""

from __future__ import annotations

from enum import StrEnum


class UnitEnum(StrEnum):
    """A unit of measure that multiplies and divides, like a currency."""


class NominalEnum(StrEnum):
    """A category compared only by equality, like a country or region code."""
