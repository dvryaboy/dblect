"""dblect's declaration layer: domain types, model contracts, and the bridge
that turns them into facts the lineage engine can act on.

A project declares meaning here, in Python beside its dbt project: domain types
built from SQL primitives, bound to models by contracts. The framework reads the
classes as schemas (it never instantiates them), resolves them against the dbt
manifest, and feeds the resulting facts to the lineage engine. See
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
    ResolvedPredicate,
    contract_fd_discoverer,
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
from dblect.types.scalars import (
    BigInt,
    Count,
    Date,
    Decimal,
    FieldDef,
    FieldKind,
    Float,
    Integer,
    Timestamp,
    Varchar,
)

__all__ = [
    "BigInt",
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
    "Float",
    "ForeignKey",
    "ForeignKeyDecl",
    "ForeignKeyEdge",
    "Integer",
    "IssueCode",
    "ModelContract",
    "NominalEnum",
    "PrimaryKey",
    "PrimaryKeyDecl",
    "ResolvedContracts",
    "ResolvedPredicate",
    "ScalarDecl",
    "Timestamp",
    "UnitEnum",
    "Varchar",
    "active_registry",
    "contract_fd_discoverer",
    "contract_key_discoverer",
    "contract_tag_discoverer",
    "dbt_relationship_edges",
    "domain_tag",
    "foreign_key_edges",
    "isolated_registry",
    "resolve_contracts",
]
