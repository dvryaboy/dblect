"""The one error the authoring layer raises while reading declarations.

A misuse the framework catches at *authoring* time (refining a field that does
not exist, fixing a magnitude to a literal, combining two facets that disagree)
raises this. A mismatch the framework catches at *resolution* time (a dbt model
that does not exist, an out-of-domain currency literal) is a finding instead, so
one broken declaration does not blind the audit to the rest. See
``docs/design/declaration-dsl.md``.
"""

from __future__ import annotations


class DomainTypeError(Exception):
    """A declaration the authoring layer cannot read as a coherent schema."""
