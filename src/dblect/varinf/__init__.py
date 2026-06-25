"""Var inference: discover every ``var()`` / ``env_var()`` reference in a dbt
project and infer enough about each to enumerate worlds.

This package is the discovery half of the flag system. The Jinja front end
(:mod:`dblect.templating`, :mod:`dblect.varinf.walker`) turns a node's source
Jinja into :class:`~dblect.varinf.usage.VarUsage` records; later streams fold
those into typed, domain-bearing flags.
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
