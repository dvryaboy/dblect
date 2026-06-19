"""Read dbt's ``manifest.json`` into a typed, dblect-shaped view of the project.

We use ``dbt-artifacts-parser`` for the version-aware parse of the on-disk JSON
into Pydantic models (so we don't track dbt's schema churn ourselves), then
transform that into a small, stable internal representation: `Manifest`, `Node`,
`Column`, and `ResourceType`. Downstream modules (audit, lineage, contracts)
import from here and don't touch the parser's types directly.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self, cast

from dbt_artifacts_parser.parser import parse_manifest  # type: ignore[import-untyped]

from dblect.manifest.dag import Dag

if TYPE_CHECKING:
    from dblect.manifest.catalog import Catalog


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
class ModelConfig:
    """The slice of a node's resolved ``config`` block dblect reasons about.

    Carries only the keys a property currently consumes, not the whole config.
    ``unique_key`` is normalized to a tuple of column names: dbt accepts it as a
    single string or a list, and both reduce to the same candidate-key shape.
    More keys (``on_schema_change``, ``cluster_by`` ...) land here as the
    properties that read them are added.
    """

    materialized: str | None = None
    incremental_strategy: str | None = None
    unique_key: tuple[str, ...] = ()
    snapshot_validity_columns: tuple[str, ...] = ()
    """A snapshot's SCD-2 validity columns (valid-from, valid-to), resolved.

    dbt names these ``dbt_valid_from`` / ``dbt_valid_to`` by default and lets a
    snapshot rename them via ``snapshot_meta_column_names``; this carries the
    effective names so consumers reason about the columns the warehouse actually
    has. Empty for non-snapshot nodes (the config block has no such key).
    """


@dataclass(frozen=True, slots=True)
class Column:
    """A column on a dbt model/source as declared in schema.yml."""

    name: str
    data_type: str | None
    description: str | None
    constraints: tuple[ConstraintSpec, ...] = ()


@dataclass(frozen=True, slots=True)
class Macro:
    """A macro definition as carried in the manifest's ``macros`` block.

    The source body (`macro_sql`) is what the var-inference macro-following
    engine expands to reach `var()` calls; `depends_on_macros` is the
    macro-to-macro edge set that walk recurses through. This view is a faithful
    transcription of the manifest entry; resolving a name to a definition (the
    bare-name-then-package lookup) lands with macro-following, which defines what
    that resolution must satisfy.
    """

    unique_id: str
    name: str
    package_name: str
    macro_sql: str
    depends_on_macros: frozenset[str] = field(
        default_factory=cast("type[frozenset[str]]", frozenset)
    )


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
    config: ModelConfig | None = None
    """The dblect-relevant slice of the node's resolved ``config`` block.

    Present for data-flow nodes whose config dblect reads (models carry the
    incremental keys); ``None`` for sources and for nodes with no config block.
    """
    identifier: str | None = None
    """The relation name as it appears in compiled SQL.

    Populated for sources (where it can diverge from ``name`` via the
    ``identifier`` setting in ``schema.yml``). ``None`` for nodes that
    don't have a separate identifier concept; callers that need a
    SQL-level lookup name should prefer ``identifier or name``.
    """

    @property
    def is_data_flow(self) -> bool:
        """True for nodes that participate in lineage (models, sources, seeds, snapshots)."""
        return self.resource_type is not ResourceType.OTHER

    @property
    def analysis_sql(self) -> str | None:
        """The SQL the analysis layer should parse for this node, or `None`.

        Always the dbt-rendered ``compiled_code``: the analysis layer needs
        macros expanded and refs resolved for its detectors to see the real
        structure. The raw template is not a usable fallback (macro calls
        come out as opaque sentinels and the detectors miss anything the
        macros emit).
        """
        return self.compiled_code


# The unique_id prefixes dbt gives the data-flow node kinds, derived from the
# resource types so the two stay in lockstep ("model.", "source.", "seed.",
# "snapshot."). ``OTHER`` (tests, analyses) never anchors a generic test.
DATA_FLOW_UID_PREFIXES: tuple[str, ...] = tuple(
    f"{rt.value}." for rt in ResourceType if rt is not ResourceType.OTHER
)


def generic_test_target_uid(
    node: Node, *, eligible_prefixes: tuple[str, ...] = DATA_FLOW_UID_PREFIXES
) -> str | None:
    """The unique_id a generic test is attached to, or None if undeterminable.

    Prefer ``attached_node`` (the modern manifest shape); fall back to the first
    eligible entry in ``depends_on`` for older manifests where ``attached_node``
    isn't populated. ``eligible_prefixes`` narrows which target kinds count, so a
    caller that grounds only models and sources can pass a subset of the
    data-flow default.
    """
    if node.attached_node and node.attached_node.startswith(eligible_prefixes):
        return node.attached_node
    for dep in sorted(node.depends_on):
        if dep.startswith(eligible_prefixes):
            return dep
    return None


@dataclass(frozen=True, slots=True)
class Manifest:
    """A dblect-shaped view of a parsed dbt ``manifest.json``."""

    schema_version: str
    adapter_type: str
    nodes: Mapping[str, Node]
    macros: Mapping[str, Macro] = field(default_factory=cast("type[dict[str, Macro]]", dict))
    """Macro definitions keyed by unique_id, the input macro-following expands.

    Defaults to empty so manifests built before this view existed (and tests
    that construct a `Manifest` directly) stay valid without restating it.
    """

    @classmethod
    def from_file(cls, path: Path) -> Self:
        """Load and parse a ``manifest.json`` file at `path`."""
        raw = json.loads(path.read_text())
        return cls.from_raw(raw)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> Self:
        """Parse a raw manifest dict (already loaded as JSON)."""
        raw = _drop_unmodeled_supported_languages(raw)
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

        # Macros live under their own top-level `macros` block, separate from
        # the data-flow `nodes`. They carry no edges into the DAG; the registry
        # is consumed by macro-following, not by lineage.
        macros = {uid: _macro_from_parsed(uid, m) for uid, m in (parsed.macros or {}).items()}
        return cls(
            schema_version=schema_version,
            adapter_type=adapter_type,
            nodes=nodes,
            macros=macros,
        )

    def merge_catalog(self, catalog: Catalog) -> Self:
        """Return a copy whose node column sets are unioned with the catalog's
        warehouse-introspected columns, so DAG leaves (seeds, sources) that no
        ``schema.yml`` documents still carry a column universe.

        Documented columns stay authoritative: a column the manifest already
        carries keeps its declared :class:`Column` (matching by name
        case-insensitively, since a warehouse may report a different case than
        the project wrote), and the catalog adds only columns the node lacks.
        Nodes the catalog does not cover pass through untouched.
        """
        merged: dict[str, Node] = {}
        for uid, node in self.nodes.items():
            catalog_columns = catalog.columns_by_uid.get(uid)
            if not catalog_columns:
                merged[uid] = node
                continue
            columns = dict(node.columns)
            # Track present names case-insensitively, seeded from the documented
            # columns and grown as catalog columns land, so two catalog entries
            # that differ only in case (a warehouse reporting both) collapse to
            # the first rather than duplicating.
            present = {name.lower() for name in columns}
            for col_name, data_type in catalog_columns.items():
                if col_name.lower() in present:
                    continue
                columns[col_name] = Column(name=col_name, data_type=data_type, description=None)
                present.add(col_name.lower())
            merged[uid] = replace(node, columns=columns)
        return replace(self, nodes=merged)

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
        config=_model_config_from_parsed(n),
    )


def _macro_from_parsed(uid: str, m: Any) -> Macro:
    """Map a dbt-artifacts-parser macro (any schema version) into our `Macro`."""
    depends_on = getattr(m, "depends_on", None)
    depends_on_macros: tuple[str, ...] = ()
    if depends_on is not None:
        depends_on_macros = tuple(getattr(depends_on, "macros", ()) or ())
    return Macro(
        unique_id=uid,
        name=m.name,
        package_name=m.package_name,
        macro_sql=m.macro_sql,
        depends_on_macros=frozenset(depends_on_macros),
    )


# The macro/node ``supported_languages`` values dbt-artifacts-parser models. dbt
# 1.9+ ships a ``function`` materialization macro that also lists ``javascript``,
# which the parser's enum rejects. dblect never reads this field, so unmodeled
# values are dropped before the parse rather than allowed to fail it, keeping the
# parse total in the same spirit as the ``from_raw`` enums above. Remove this once
# the parser models the value (tracked in #106; upstream dbt-artifacts-parser#219).
_MODELED_SUPPORTED_LANGUAGES = frozenset({"python", "sql"})


def _drop_unmodeled_supported_languages(raw: dict[str, Any]) -> dict[str, Any]:
    """Return ``raw`` with ``supported_languages`` under ``macros`` and ``nodes``
    filtered to the values the manifest parser models.

    The input is left untouched: only the sections and entries that actually carry
    an unmodeled value are copied (copy-on-write), and ``raw`` itself is returned
    unchanged when nothing needs filtering, so a caller that reuses the dict is not
    surprised by a mutation.
    """
    patched: dict[str, Any] | None = None
    for section in ("macros", "nodes"):
        entries = raw.get(section)
        if not isinstance(entries, dict):
            continue
        entries_typed = cast("dict[str, Any]", entries)
        new_entries: dict[str, Any] | None = None
        for uid, entry in entries_typed.items():
            if not isinstance(entry, dict):
                continue
            entry_typed = cast("dict[str, Any]", entry)
            languages = entry_typed.get("supported_languages")
            if not isinstance(languages, list):
                continue
            kept = [
                language
                for language in cast("list[Any]", languages)
                if language in _MODELED_SUPPORTED_LANGUAGES
            ]
            if kept == languages:
                continue
            if new_entries is None:
                new_entries = dict(entries_typed)
            new_entries[uid] = {**entry_typed, "supported_languages": kept}
        if new_entries is not None:
            if patched is None:
                patched = dict(raw)
            patched[section] = new_entries
    return patched if patched is not None else raw


def _source_from_parsed(uid: str, s: Any) -> Node:
    """Sources have no `raw_code`/`compiled_code` and no `depends_on`.

    ``identifier`` is the relation name dbt resolves ``{{ source(...) }}``
    to in compiled SQL; it defaults to ``name`` in the v12 schema but may
    differ when the schema.yml sets it explicitly.
    """
    raw_identifier = getattr(s, "identifier", None)
    identifier = raw_identifier if isinstance(raw_identifier, str) and raw_identifier else None
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
        identifier=identifier,
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


def _model_config_from_parsed(node: Any) -> ModelConfig | None:
    """Build :class:`ModelConfig` from a parsed node's ``config`` block, or ``None``.

    Returns ``None`` when the node has no config block (sources, and any node the
    parser leaves config-less). ``unique_key`` is normalized from dbt's string or
    list shape to a tuple of the string column names, dropping any non-string
    entry rather than failing on a malformed list.
    """
    config = getattr(node, "config", None)
    if config is None:
        return None
    materialized = _opt_str(getattr(config, "materialized", None))
    strategy = _opt_str(getattr(config, "incremental_strategy", None))
    unique_key = _normalize_unique_key(getattr(config, "unique_key", None))
    return ModelConfig(
        materialized=materialized,
        incremental_strategy=strategy,
        unique_key=unique_key,
        snapshot_validity_columns=_snapshot_validity_columns(
            getattr(config, "snapshot_meta_column_names", None)
        ),
    )


# dbt's default snapshot validity column names, used for any key
# ``snapshot_meta_column_names`` leaves unset (the common case: the block exists
# with null values until a snapshot opts into renaming).
_DEFAULT_VALID_FROM = "dbt_valid_from"
_DEFAULT_VALID_TO = "dbt_valid_to"

# The default (valid-from, valid-to) pair as one value, for callers that need a
# snapshot's validity columns when its config carries no ``snapshot_meta_column_names``
# block at all (older manifests, where ``_snapshot_validity_columns`` yields ``()``).
DEFAULT_SNAPSHOT_VALIDITY_COLUMNS: tuple[str, str] = (_DEFAULT_VALID_FROM, _DEFAULT_VALID_TO)


def _snapshot_validity_columns(raw: Any) -> tuple[str, ...]:
    """Resolve a snapshot's (valid-from, valid-to) column names from its
    ``snapshot_meta_column_names`` config, or ``()`` when the node is not a snapshot.

    dbt attaches this block only to snapshots, with each entry either a renamed
    column name or null (use the default). A non-string entry falls back to the
    default, so a partial rename still yields two usable names."""
    if raw is None:
        return ()

    def resolve(key: str, default: str) -> str:
        if isinstance(raw, Mapping):
            value = cast("Mapping[str, Any]", raw).get(key)
        else:
            value = getattr(raw, key, None)
        return value if isinstance(value, str) and value else default

    return (
        resolve("dbt_valid_from", _DEFAULT_VALID_FROM),
        resolve("dbt_valid_to", _DEFAULT_VALID_TO),
    )


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _normalize_unique_key(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        return (raw,) if raw else ()
    if isinstance(raw, (list, tuple)):
        seq = tuple(cast("Iterable[object]", raw))
        return tuple(c for c in seq if isinstance(c, str) and c)
    return ()


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
