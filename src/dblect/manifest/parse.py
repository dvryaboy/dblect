"""Read dbt's ``manifest.json`` into a typed, dblect-shaped view of the project.

We use ``dbt-artifacts-parser`` for the version-aware parse of the on-disk JSON
into Pydantic models (so we don't track dbt's schema churn ourselves), then
transform that into a small, stable internal representation: `Manifest`, `Node`,
`Column`, and `ResourceType`. Downstream modules (audit, lineage, contracts)
import from here and don't touch the parser's types directly.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Self, cast

from dbt_artifacts_parser.parser import parse_manifest  # type: ignore[import-untyped]

from dblect.manifest.dag import Dag


class ResourceType(StrEnum):
    """dbt node kinds dblect cares about for data-flow analysis.

    `OTHER` covers nodes (tests, analyses, operations, unit_tests) that we
    surface as part of the manifest but don't currently treat as part of the
    data-flow DAG. Edges to/from these are still recorded; the distinction is
    purely for filtering convenience.
    """

    MODEL = "model"
    SOURCE = "source"
    SEED = "seed"
    SNAPSHOT = "snapshot"
    OTHER = "other"

    @classmethod
    def from_raw(cls, raw: str) -> ResourceType:
        try:
            return cls(raw)
        except ValueError:
            return cls.OTHER


class ConstraintType(StrEnum):
    """The kinds of constraints dbt 1.5+ understands.

    ``OTHER`` covers vendor- or dialect-specific constraint types we don't
    recognise so the parse stays total. Comparisons should go through the
    enum members rather than raw strings to keep typos out of the call sites.
    """

    PRIMARY_KEY = "primary_key"
    UNIQUE = "unique"
    NOT_NULL = "not_null"
    CHECK = "check"
    FOREIGN_KEY = "foreign_key"
    OTHER = "other"

    @classmethod
    def from_raw(cls, raw: str) -> ConstraintType:
        try:
            return cls(raw.lower())
        except ValueError:
            return cls.OTHER


@dataclass(frozen=True, slots=True)
class ConstraintSpec:
    """A constraint declared on a model or column in schema.yml (dbt 1.5+).

    ``columns`` carries the column set for model-level constraints; column-
    level constraints leave it empty (the column they're attached to is
    implicit). ``expression`` carries a CHECK constraint's predicate text,
    if any.
    """

    type: ConstraintType
    columns: tuple[str, ...] = ()
    expression: str | None = None


@dataclass(frozen=True, slots=True)
class DbtTestMetadata:
    """What dblect knows about a dbt test node.

    Mostly mirrors dbt's ``test_metadata`` block on the node (``name``,
    ``kwargs``, ``namespace``), enriched with the test-relevant slice of
    node-level config (``enabled``, ``where``) so consumers can reason
    about test semantics from one place.

    * ``name``: generic-test name (``"unique"``, ``"not_null"``,
      ``"dbt_utils.unique_combination_of_columns"``, etc.).
    * ``kwargs``: the arguments the test was instantiated with,
      heterogeneously shaped per test type (``column_name`` for
      ``unique``, ``combination_of_columns`` for
      ``unique_combination_of_columns``, and so on).
    * ``namespace``: the package the test comes from (``"dbt_utils"``,
      etc.). ``None`` for dbt-built-in tests.
    * ``enabled``: from ``node.config.enabled``. Defaults to ``True`` when
      unset.
    * ``where``: from ``node.config.where``. The filter the test runs
      under; a non-``None`` value means the test only asserts its
      property over rows matching ``where``, so any fact derived from it
      is conditional.
    """

    name: str
    kwargs: Mapping[str, Any]
    namespace: str | None = None
    enabled: bool = True
    where: str | None = None


@dataclass(frozen=True, slots=True)
class Column:
    """A column on a dbt model/source as declared in schema.yml."""

    name: str
    data_type: str | None
    description: str | None
    constraints: tuple[ConstraintSpec, ...] = ()


@dataclass(frozen=True, slots=True)
class Node:
    """A node in the project's data-flow DAG.

    Covers models, sources, seeds, snapshots, plus an `OTHER` bucket for
    anything else dbt parses (tests, etc.). For non-data-flow nodes some
    fields (`raw_code`, `compiled_code`) are typically empty.
    """

    unique_id: str
    name: str
    resource_type: ResourceType
    fqn: tuple[str, ...]
    package_name: str
    schema: str | None
    raw_code: str | None
    compiled_code: str | None
    original_file_path: str | None
    columns: Mapping[str, Column]
    depends_on: frozenset[str] = field(default_factory=cast("type[frozenset[str]]", frozenset))
    constraints: tuple[ConstraintSpec, ...] = ()
    test_metadata: DbtTestMetadata | None = None
    attached_node: str | None = None

    @property
    def is_data_flow(self) -> bool:
        """True for nodes that participate in lineage (models, sources, seeds, snapshots)."""
        return self.resource_type is not ResourceType.OTHER


@dataclass(frozen=True, slots=True)
class Manifest:
    """A dblect-shaped view of a parsed dbt ``manifest.json``."""

    schema_version: str
    adapter_type: str
    nodes: Mapping[str, Node]

    @classmethod
    def from_file(cls, path: Path) -> Self:
        """Load and parse a ``manifest.json`` file at `path`."""
        raw = json.loads(path.read_text())
        return cls.from_raw(raw)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> Self:
        """Parse a raw manifest dict (already loaded as JSON)."""
        parsed = parse_manifest(raw)
        nodes: dict[str, Node] = {}

        # Models, seeds, snapshots, and tests all live under `nodes`.
        for uid, n in parsed.nodes.items():
            nodes[uid] = _node_from_parsed(uid, n)

        # Sources live under `sources` in the manifest. Promote them into
        # the same Node namespace so the DAG is uniform.
        for uid, s in (parsed.sources or {}).items():
            nodes[uid] = _source_from_parsed(uid, s)

        schema_version = parsed.metadata.dbt_schema_version
        if schema_version is None:
            raise ValueError("manifest is missing metadata.dbt_schema_version")
        adapter_type = getattr(parsed.metadata, "adapter_type", None)
        if not isinstance(adapter_type, str) or not adapter_type:
            raise ValueError("manifest is missing metadata.adapter_type")
        return cls(
            schema_version=schema_version,
            adapter_type=adapter_type,
            nodes=nodes,
        )

    @property
    def models(self) -> Mapping[str, Node]:
        return self._by_kind(ResourceType.MODEL)

    @property
    def sources(self) -> Mapping[str, Node]:
        return self._by_kind(ResourceType.SOURCE)

    @property
    def seeds(self) -> Mapping[str, Node]:
        return self._by_kind(ResourceType.SEED)

    @property
    def snapshots(self) -> Mapping[str, Node]:
        return self._by_kind(ResourceType.SNAPSHOT)

    @property
    def dag(self) -> Dag:
        """The full project DAG including every parsed node.

        Edges are taken from each node's `depends_on`. Edges to nodes not
        present in the manifest (e.g., upstream models from packages the
        manifest didn't expose) are silently dropped.
        """
        node_ids = set(self.nodes)
        edges = [
            (upstream, n.unique_id)
            for n in self.nodes.values()
            for upstream in n.depends_on
            if upstream in node_ids
        ]
        return Dag.build(node_ids, edges)

    def _by_kind(self, kind: ResourceType) -> Mapping[str, Node]:
        return {uid: n for uid, n in self.nodes.items() if n.resource_type is kind}


def _node_from_parsed(uid: str, n: Any) -> Node:
    """Map a dbt-artifacts-parser node (any schema version) into our `Node`."""
    raw_code = getattr(n, "raw_code", None)
    compiled_code = getattr(n, "compiled_code", None)
    schema = getattr(n, "schema", None)
    depends_on_nodes = ()
    depends_on = getattr(n, "depends_on", None)
    if depends_on is not None:
        depends_on_nodes = tuple(getattr(depends_on, "nodes", ()) or ())
    return Node(
        unique_id=uid,
        name=n.name,
        resource_type=ResourceType.from_raw(str(n.resource_type)),
        fqn=tuple(n.fqn),
        package_name=n.package_name,
        schema=schema,
        raw_code=raw_code,
        compiled_code=compiled_code,
        original_file_path=getattr(n, "original_file_path", None),
        columns=_columns_from_parsed(getattr(n, "columns", {}) or {}),
        depends_on=frozenset(depends_on_nodes),
        constraints=_constraints_from_parsed(getattr(n, "constraints", None) or ()),
        test_metadata=_test_metadata_from_parsed(n),
        attached_node=getattr(n, "attached_node", None),
    )


def _source_from_parsed(uid: str, s: Any) -> Node:
    """Sources have no `raw_code`/`compiled_code` and no `depends_on`."""
    return Node(
        unique_id=uid,
        name=s.name,
        resource_type=ResourceType.SOURCE,
        fqn=tuple(s.fqn),
        package_name=s.package_name,
        schema=getattr(s, "schema", None),
        raw_code=None,
        compiled_code=None,
        original_file_path=getattr(s, "original_file_path", None),
        columns=_columns_from_parsed(getattr(s, "columns", {}) or {}),
        depends_on=frozenset(),
    )


def _columns_from_parsed(raw: Mapping[str, Any]) -> Mapping[str, Column]:
    return {
        name: Column(
            name=col.name,
            data_type=getattr(col, "data_type", None),
            description=getattr(col, "description", None),
            constraints=_constraints_from_parsed(getattr(col, "constraints", None) or ()),
        )
        for name, col in raw.items()
    }


def _constraints_from_parsed(raw: Any) -> tuple[ConstraintSpec, ...]:
    return tuple(
        ConstraintSpec(
            type=ConstraintType.from_raw(str(getattr(c, "type", ""))),
            columns=tuple(getattr(c, "columns", None) or ()),
            expression=getattr(c, "expression", None),
        )
        for c in raw
    )


def _test_metadata_from_parsed(node: Any) -> DbtTestMetadata | None:
    """Build `DbtTestMetadata` from a parsed dbt test node, or `None`.

    Reads the test_metadata block (``name``, ``kwargs``, ``namespace``) and
    the test-relevant slice of node config (``enabled``, ``where``). Returns
    `None` when the node has no test_metadata block or its name is missing,
    which is the case for every non-test node.
    """
    raw = getattr(node, "test_metadata", None)
    if raw is None:
        return None
    name = getattr(raw, "name", None)
    if not isinstance(name, str):
        return None
    raw_kwargs: Any = getattr(raw, "kwargs", None) or {}
    kwargs: dict[str, Any] = {}
    if isinstance(raw_kwargs, Mapping):
        raw_mapping = cast("Mapping[Any, Any]", raw_kwargs)
        kwargs = {str(k): v for k, v in raw_mapping.items()}
    raw_namespace = getattr(raw, "namespace", None)
    namespace = raw_namespace if isinstance(raw_namespace, str) and raw_namespace else None
    config = getattr(node, "config", None)
    raw_enabled = getattr(config, "enabled", True)
    enabled = bool(raw_enabled) if raw_enabled is not None else True
    raw_where = getattr(config, "where", None)
    where = raw_where if isinstance(raw_where, str) and raw_where else None
    return DbtTestMetadata(
        name=name,
        kwargs=kwargs,
        namespace=namespace,
        enabled=enabled,
        where=where,
    )
