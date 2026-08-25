"""Var inference: find every ``var()`` / ``env_var()`` reference in a dbt
project and work out enough about each one to check the project under every
value it could take.

This package is the discovery half of dblect's flag system, which turns dbt
vars and env vars into typed configuration. The Jinja front end
(:mod:`dblect.templating`, :mod:`dblect.varinf.walker`) turns a node's source
Jinja into :class:`~dblect.varinf.usage.VarUsage` records; later stages turn
those into flags with a declared type and a known set of values.
"""

from dblect.varinf.usage import (
    Arithmetic,
    ArithOp,
    Comparison,
    ComparisonOp,
    Confidence,
    InSet,
    LiteralPosition,
    LiteralValue,
    MacroArg,
    OpaqueNode,
    SourceLocation,
    SqlLiteral,
    TruthyTest,
    Unknown,
    UsageContext,
    VarKind,
    VarUsage,
    WalkResult,
)
from dblect.varinf.walker import walk_source

__all__ = [
    "ArithOp",
    "Arithmetic",
    "Comparison",
    "ComparisonOp",
    "Confidence",
    "InSet",
    "LiteralPosition",
    "LiteralValue",
    "MacroArg",
    "OpaqueNode",
    "SourceLocation",
    "SqlLiteral",
    "TruthyTest",
    "Unknown",
    "UsageContext",
    "VarKind",
    "VarUsage",
    "WalkResult",
    "walk_source",
]
