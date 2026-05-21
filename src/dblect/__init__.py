"""dblect — semantic correctness framework for dbt analytics pipelines.

Public API surfaces as we implement: SemanticType, ModelContract, SemanticFlag,
Field, ForeignKey, flag, contract, models, Equivalence, Requires.
"""

from dblect._version import __version__

__all__ = ["__version__"]
