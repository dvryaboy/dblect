"""How a window function or aggregate reads the order of the rows it folds."""

from __future__ import annotations

from enum import Enum, auto

__all__ = ["OrderUse"]


class OrderUse(Enum):
    """What a function's result depends on once its ORDER BY leaves ties.

    * ``NONE``: only the set of rows in the frame (``sum``, ``min``, ``count``).
    * ``PEER_RANK``: the row's peer group, so tied rows share one value (``rank`` family).
    * ``SEQUENCE``: the ordered sequence of its argument (``first_value``, ``array_agg``).
      Tied rows reorder the sequence, but a sequence of values the order determines is fixed.
    * ``POSITION``: the row's own place among its ties (``row_number``, ``ntile``, ``lag``).
    * ``PICKS``: a choice among tied rows (``any_value``, ``arg_max``, an unknown function).
    """

    NONE = auto()
    PEER_RANK = auto()
    SEQUENCE = auto()
    POSITION = auto()
    PICKS = auto()
