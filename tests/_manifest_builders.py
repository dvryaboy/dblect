"""The one manifest-shape vocabulary for tests that build dblect-shaped manifests inline.

Analysis tests pin contracts against hand-written SQL by constructing small
typed ``Manifest`` values directly rather than parsing a ``manifest.json``
fixture. These builders carry the shared defaults for that shape (package
``shop``, model schema ``analytics``, source schema ``raw``, ``fqn`` and
``name`` derived from the unique_id) so a test states only what it asserts
on. A test whose subject is one of the defaulted fields passes the value
explicitly; ``package_name`` and ``schema`` are carried but never read by
the analysis layer, so their defaults are inert for behavior.
"""

from __future__ import annotations

from collections.abc import Mapping

from dblect.manifest import (
    Column,
    ConstraintSpec,
    DbtTestMetadata,
    Manifest,
    ModelConfig,
    Node,
    ResourceType,
)


def cols(*names: str, **types: str) -> Mapping[str, Column]:
    """Columns keyed by name: positional names get VARCHAR, keyword names their given type."""
    out = {n: Column(name=n, data_type="VARCHAR", description=None) for n in names}
    out.update({n: Column(name=n, data_type=t, description=None) for n, t in types.items()})
    return out


def node(
    uid: str,
    sql: str | None = None,
    *,
    kind: ResourceType = ResourceType.MODEL,
    raw: str | None = None,
    name: str | None = None,
    fqn: tuple[str, ...] | None = None,
    package: str = "shop",
    schema: str | None = "analytics",
    path: str | None = None,
    columns: Mapping[str, Column] | None = None,
    depends_on: frozenset[str] = frozenset(),
    constraints: tuple[ConstraintSpec, ...] = (),
    test_metadata: DbtTestMetadata | None = None,
    attached_node: str | None = None,
    config: ModelConfig | None = None,
    identifier: str | None = None,
    compiled_flag: bool | None = None,
) -> Node:
    """A ``Node`` with defaults derived from ``uid``.

    ``sql`` is the dbt-rendered ``compiled_code`` (what the analysis layer
    parses); ``raw`` is the source template. ``name`` defaults to the last
    unique_id segment and ``fqn`` to the segments after the resource type,
    matching how dbt shapes both. A model's ``path`` defaults to
    ``models/<name>.sql``, the location dbt would give it, so a finding
    carries a realistic file path without every test restating one; the
    other kinds live elsewhere in a project and default to no path.
    """
    resolved_name = name if name is not None else uid.split(".")[-1]
    if path is None and kind is ResourceType.MODEL:
        path = f"models/{resolved_name}.sql"
    return Node(
        unique_id=uid,
        name=resolved_name,
        resource_type=kind,
        fqn=fqn if fqn is not None else tuple(uid.split(".")[1:]),
        package_name=package,
        schema=schema,
        raw_code=raw,
        compiled_code=sql,
        original_file_path=path,
        columns=columns if columns is not None else {},
        depends_on=depends_on,
        constraints=constraints,
        test_metadata=test_metadata,
        attached_node=attached_node,
        config=config,
        identifier=identifier,
        compiled_flag=compiled_flag,
    )


def source(
    uid: str,
    *,
    name: str | None = None,
    fqn: tuple[str, ...] | None = None,
    package: str = "shop",
    schema: str | None = "raw",
    columns: Mapping[str, Column] | None = None,
    identifier: str | None = None,
) -> Node:
    """A source ``Node``: a leaf relation with no SQL, living in the raw schema."""
    return node(
        uid,
        kind=ResourceType.SOURCE,
        name=name,
        fqn=fqn,
        package=package,
        schema=schema,
        columns=columns,
        identifier=identifier,
    )


def manifest(*nodes: Node, adapter_type: str = "duckdb", schema_version: str = "v12") -> Manifest:
    """A ``Manifest`` over ``nodes``, keyed by unique_id."""
    return Manifest(
        schema_version=schema_version,
        adapter_type=adapter_type,
        nodes={n.unique_id: n for n in nodes},
    )
