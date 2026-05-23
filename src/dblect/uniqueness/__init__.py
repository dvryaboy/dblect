"""Uniqueness facts derived from dbt declarations and model SQL.

This is the substrate detectors consult when they need to reason about which
column sets are unique on each model. Facts are derived opportunistically:
we use what the project tells us (tests, constraints, structural proof from
SQL, propagation through SQL operations) and stay silent where we can't
ground a claim.
"""

from dblect.uniqueness.facts import (
    UniquenessFact,
    UniquenessSource,
    facts_from_declarations,
    facts_from_manifest,
)
from dblect.uniqueness.propagation import (
    ScopeFacts,
    facts_from_tree,
    propagate_facts,
    top_scope_facts,
)

__all__ = [
    "ScopeFacts",
    "UniquenessFact",
    "UniquenessSource",
    "facts_from_declarations",
    "facts_from_manifest",
    "facts_from_tree",
    "propagate_facts",
    "top_scope_facts",
]
