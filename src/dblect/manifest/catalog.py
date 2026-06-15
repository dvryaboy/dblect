"""dbt ``catalog.json`` ingestion: warehouse-introspected columns per node.

``dbt docs generate`` writes a ``catalog.json`` next to the manifest, carrying
the column universe of every node as the warehouse reports it, including the DAG
leaves (seeds and sources) that have no SQL for dblect to derive columns from. A
project that documents none of its leaves in ``schema.yml`` still resolves
``select *`` and qualified references once the catalog supplies those columns.

This module reads only what dblect needs from the catalog (per-node column names
and their types) and leaves the merge into the manifest to
:meth:`Manifest.merge_catalog`, which keeps documented columns authoritative.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self, cast

from dbt_artifacts_parser.parser import parse_catalog  # type: ignore[import-untyped]


@dataclass(frozen=True, slots=True)
class Catalog:
    """The columns dbt introspected from the warehouse, keyed by node unique_id.

    ``columns_by_uid[uid]`` maps a column name to its reported data type (or
    ``None`` when the catalog omits one). Empty for a node the catalog did not
    cover, which is silence, not an assertion of no columns.
    """

    columns_by_uid: Mapping[str, Mapping[str, str | None]]

    @classmethod
    def from_file(cls, path: Path) -> Self:
        """Load and parse a ``catalog.json`` file at ``path``."""
        return cls.from_raw(json.loads(path.read_text()))

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> Self:
        """Parse a raw catalog dict (already loaded as JSON).

        Nodes and sources share the same per-node shape (a ``columns`` map of
        name to a metadata object carrying ``type``), so both fold into one
        index keyed by unique_id."""
        parsed = parse_catalog(raw)
        by_uid: dict[str, dict[str, str | None]] = {}
        sections: tuple[Mapping[str, Any], ...] = (
            cast("Mapping[str, Any]", getattr(parsed, "nodes", None) or {}),
            cast("Mapping[str, Any]", getattr(parsed, "sources", None) or {}),
        )
        for section in sections:
            for uid, entry in section.items():
                columns = cast("Mapping[str, Any]", getattr(entry, "columns", None) or {})
                cols: dict[str, str | None] = {}
                for name, col in columns.items():
                    data_type = getattr(col, "type", None)
                    cols[name] = data_type if isinstance(data_type, str) else None
                if cols:
                    by_uid[uid] = cols
        return cls(columns_by_uid=by_uid)
