# pyright: reportInvalidTypeForm=false
"""The standard domain types every project starts from.

``Money`` is the worked example throughout the design: an ``amount`` and the
``currency`` it is denominated in, so a sum that mixes currencies stops being
well-typed. A project pins the currency in the type for a single-currency model
(``Money.refine(currency=Currency.USD)``) or leaves it open and maps it to a
column for a multi-currency one. See ``docs/design/declaration-dsl.md``.
"""

from __future__ import annotations

from dblect.types.domain import DomainType
from dblect.types.enums import Currency
from dblect.types.scalars import Decimal


class Money(DomainType):
    """An amount of money in some currency."""

    amount: Decimal(18, 2)
    currency: Currency
