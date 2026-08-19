"""How a GROUP BY names its targets, shared by the generators that vary that axis.

Both lattice soundness PBTs (uniqueness and functional dependency) ground a fact from the
group key, so both want the same spellings drawn against the same oracle.
"""

from __future__ import annotations

from enum import StrEnum


class GroupSpelling(StrEnum):
    """How a GROUP BY names its targets.

    ``EXPRESSION`` and ``ORDINAL`` denote the same grouping, so a lattice must ground the
    same fact for either. ``SHADOWING_ALIAS`` does not: there the name is a real input
    column that the projection also aliases over, so SQL's input-before-alias binding keeps
    the two targets distinct, while reading the name as its projection collapses them onto
    one column. A collapsed two-column group key becomes a one-column key, the stronger
    claim, which an execution oracle sees as a duplicate in the output.
    """

    EXPRESSION = "expression"
    ORDINAL = "ordinal"
    SHADOWING_ALIAS = "shadowing_alias"
