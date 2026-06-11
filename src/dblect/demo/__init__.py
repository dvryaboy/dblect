"""Starter vocabulary for the walkthrough: ready-made domain types and enums.

This is illustrative material, not a maintained standard library. The ISO 4217
and ISO 3166-1 slices here are partial by design, enough to drive the
currency-creep walkthrough (``docs/design/demo_walkthrough.md``). Copy what you
need into your own project and extend it: a project's real units and categories
are its own to declare, by subclassing the ``UnitEnum`` / ``NominalEnum`` markers
that live in ``dblect.types``.
"""

from __future__ import annotations

from dblect.demo.enums import Country, Currency
from dblect.demo.library import Money

__all__ = [
    "Country",
    "Currency",
    "Money",
]
