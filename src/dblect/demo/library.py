# pyright: reportInvalidTypeForm=false
"""``Money``: the worked example domain type for the walkthrough.

An ``amount`` and the ``currency`` it is denominated in, so a sum that mixes
currencies stops being well-typed. A single-currency model pins the currency
(``Money.refine(currency=Currency.USD)``); a multi-currency one leaves it open
and maps it to a column. Illustrative starter material, not a maintained
library. See ``docs/design/declaration-dsl.md``.
"""

from __future__ import annotations

from dblect.demo.enums import Currency
from dblect.types import Decimal, DomainType


class Money(DomainType):
    """An amount of money in some currency."""

    amount: Decimal(18, 2)
    currency: Currency
