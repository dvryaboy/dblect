"""Marker bases that classify a domain type's enum fields by how the substrate
carries them.

A :class:`UnitEnum` is a *dimensional* unit (a currency is the worked example),
so it rides in a tag's dimensional monomial and does exponent arithmetic under
``*`` and ``/``. A :class:`NominalEnum` is a *nominal* category (a country
code), carried by equality only. Both subclass :class:`enum.StrEnum`, so a
member equals its string code and ``MyUnit("USD")`` round-trips a literal, which
is what lets a contract accept ``currency="USD"`` and ``currency=MyUnit.USD``
alike (an out-of-domain literal is a finding, raised by neither the enum nor
here).

A project declares its own vocabularies by subclassing these. The
``dblect.demo`` package ships partial ISO 4217 / 3166-1 slices to drive the
walkthrough.
"""

from __future__ import annotations

from enum import StrEnum


class UnitEnum(StrEnum):
    """A dimensional unit: a category that multiplies and divides (a currency)."""


class NominalEnum(StrEnum):
    """A nominal category carried by equality (a country, a region code)."""
