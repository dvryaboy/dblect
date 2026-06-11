"""The dblect declaration layer: domain types, model contracts, and the bridge
that turns them into substrate facts.

A project declares meaning here, in Python beside its dbt project: domain types
built from SQL primitives, bound to models by contracts. The framework reads the
classes as schemas (never instantiating them), resolves them against the dbt
manifest, and feeds the lineage engine the facts it propagates. See
``docs/design/declaration-dsl.md``.
"""

from __future__ import annotations

from dblect.types.bridge import (
    BoundTag,
    ColumnConstraint,
    ContractIssue,
    ForeignKeyEdge,
    IssueCode,
    ResolvedContracts,
    contract_key_discoverer,
    contract_tag_discoverer,
    dbt_relationship_edges,
    domain_tag,
    foreign_key_edges,
    resolve_contracts,
)
from dblect.types.contract import (
    Constraints,
    ContractField,
    ContractRegistry,
    ContractSpec,
    DomainDecl,
    Field,
    ForeignKey,
    ForeignKeyDecl,
    ModelContract,
    PrimaryKey,
    PrimaryKeyDecl,
    ScalarDecl,
    active_registry,
    isolated_registry,
)
from dblect.types.domain import DomainSpec, DomainType, DomainTypeMeta
from dblect.types.enums import NominalEnum, UnitEnum
from dblect.types.errors import DomainTypeError
from dblect.types.scalars import Count, Date, Decimal, FieldDef, FieldKind, Varchar

__all__ = [
    "BoundTag",
    "ColumnConstraint",
    "Constraints",
    "ContractField",
    "ContractIssue",
    "ContractRegistry",
    "ContractSpec",
    "Count",
    "Date",
    "Decimal",
    "DomainDecl",
    "DomainSpec",
    "DomainType",
    "DomainTypeError",
    "DomainTypeMeta",
    "Field",
    "FieldDef",
    "FieldKind",
    "ForeignKey",
    "ForeignKeyDecl",
    "ForeignKeyEdge",
    "IssueCode",
    "ModelContract",
    "NominalEnum",
    "PrimaryKey",
    "PrimaryKeyDecl",
    "ResolvedContracts",
    "ScalarDecl",
    "UnitEnum",
    "Varchar",
    "active_registry",
    "contract_key_discoverer",
    "contract_tag_discoverer",
    "dbt_relationship_edges",
    "domain_tag",
    "foreign_key_edges",
    "isolated_registry",
    "resolve_contracts",
]
