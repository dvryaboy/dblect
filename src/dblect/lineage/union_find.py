"""A small union-find over hashable items, for grouping values proved equal."""

from __future__ import annotations

from collections.abc import Hashable
from typing import Generic, TypeVar

T = TypeVar("T", bound=Hashable)


class UnionFind(Generic[T]):
    def __init__(self) -> None:
        self._parent: dict[T, T] = {}

    def find(self, item: T) -> T:
        root = item
        while (up := self._parent.get(root, root)) != root:
            root = up
        while item != root:
            self._parent[item], item = root, self._parent.get(item, root)
        return root

    def union(self, a: T, b: T) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb
