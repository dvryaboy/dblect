"""Immutable directed acyclic graph keyed by node identifier.

Used by the manifest module to represent dbt's project structure, and reused by
downstream consumers (type propagation, change-impact) that need topological
queries over the project DAG.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Self


class CycleError(ValueError):
    """Raised when a DAG operation discovers a cycle.

    `cycle` lists the node identifiers in the order they were traversed when
    the cycle was found, useful for error messages but not a unique canonical form.
    """

    cycle: tuple[str, ...]

    def __init__(self, cycle: tuple[str, ...]) -> None:
        super().__init__(f"cycle detected: {' -> '.join(cycle)}")
        self.cycle = cycle


@dataclass(frozen=True, slots=True)
class Dag:
    """Immutable DAG over string node identifiers.

    Edges run upstream → downstream. The constructor verifies acyclicity; once
    built, all queries are O(1) for direct neighbors and O(V + E) for transitive
    queries.
    """

    nodes: frozenset[str]
    _upstream: Mapping[str, frozenset[str]]
    _downstream: Mapping[str, frozenset[str]]

    @classmethod
    def build(cls, nodes: Iterable[str], edges: Iterable[tuple[str, str]]) -> Self:
        """Construct a DAG from explicit nodes and (upstream, downstream) edges.

        Both endpoints of every edge must appear in `nodes`. Cycles raise
        `CycleError` rather than producing a malformed object.
        """
        node_set = frozenset(nodes)
        up: dict[str, set[str]] = {n: set() for n in node_set}
        down: dict[str, set[str]] = {n: set() for n in node_set}
        for upstream, downstream in edges:
            if upstream not in node_set:
                raise ValueError(f"edge references unknown node: {upstream!r}")
            if downstream not in node_set:
                raise ValueError(f"edge references unknown node: {downstream!r}")
            up[downstream].add(upstream)
            down[upstream].add(downstream)

        dag = cls(
            nodes=node_set,
            _upstream={n: frozenset(up[n]) for n in node_set},
            _downstream={n: frozenset(down[n]) for n in node_set},
        )
        # Validate acyclicity by attempting a topological sort.
        dag.topological_order()
        return dag

    def upstream(self, node: str) -> frozenset[str]:
        """Direct upstream neighbors (nodes this node depends on)."""
        self._require(node)
        return self._upstream[node]

    def downstream(self, node: str) -> frozenset[str]:
        """Direct downstream neighbors (nodes that depend on this node)."""
        self._require(node)
        return self._downstream[node]

    def transitive_upstream(self, node: str) -> frozenset[str]:
        """All ancestors of `node` (excluding `node` itself)."""
        self._require(node)
        return self._traverse(node, self._upstream)

    def transitive_downstream(self, node: str) -> frozenset[str]:
        """All descendants of `node` (excluding `node` itself)."""
        self._require(node)
        return self._traverse(node, self._downstream)

    def topological_order(self) -> tuple[str, ...]:
        """Return nodes in a topological order (upstream before downstream).

        Order is deterministic for a given DAG: ties are broken by node-id sort.
        """
        # Kahn's algorithm with deterministic tie-breaking via sorted node order.
        indegree = {n: len(self._upstream[n]) for n in self.nodes}
        ready = sorted(n for n, d in indegree.items() if d == 0)
        ordered: list[str] = []
        while ready:
            n = ready.pop(0)
            ordered.append(n)
            for downstream in sorted(self._downstream[n]):
                indegree[downstream] -= 1
                if indegree[downstream] == 0:
                    # Insert in sorted position to keep deterministic order.
                    self._insert_sorted(ready, downstream)
        if len(ordered) != len(self.nodes):
            # Some nodes have residual in-degree → cycle. Surface one of them.
            stuck = next(n for n, d in indegree.items() if d > 0)
            raise CycleError(self._find_cycle_from(stuck))
        return tuple(ordered)

    def _require(self, node: str) -> None:
        if node not in self.nodes:
            raise KeyError(node)

    def _traverse(self, start: str, edges: Mapping[str, frozenset[str]]) -> frozenset[str]:
        visited: set[str] = set()
        stack = list(edges[start])
        while stack:
            n = stack.pop()
            if n in visited:
                continue
            visited.add(n)
            stack.extend(edges[n])
        return frozenset(visited)

    def _find_cycle_from(self, start: str) -> tuple[str, ...]:
        """DFS to recover a witness cycle for `CycleError`."""
        path: list[str] = []
        on_path: set[str] = set()

        def dfs(node: str) -> tuple[str, ...] | None:
            if node in on_path:
                idx = path.index(node)
                return (*path[idx:], node)
            on_path.add(node)
            path.append(node)
            for nxt in sorted(self._downstream[node]):
                found = dfs(nxt)
                if found is not None:
                    return found
            path.pop()
            on_path.remove(node)
            return None

        return dfs(start) or (start,)

    @staticmethod
    def _insert_sorted(ready: list[str], item: str) -> None:
        # Linear insert: `ready` is small in practice. Swap for bisect if profiling says so.
        for i, existing in enumerate(ready):
            if item < existing:
                ready.insert(i, item)
                return
        ready.append(item)
