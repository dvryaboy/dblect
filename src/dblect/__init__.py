"""dblect: semantic correctness framework for dbt analytics pipelines.

The authored surface a project writes against: domain types and model contracts
(also under ``dblect.types``), and the contract/proxy layer (``contract``,
``models``) for relating columns across a model's rows and across models. More
surfaces (flags, the CLI) land as they are built.
"""

from dblect._version import __version__
from dblect.contracts import contract, models
from dblect.types import (
    DomainType,
    Field,
    ForeignKey,
    ModelContract,
    PrimaryKey,
)

__all__ = [
    "DomainType",
    "Field",
    "ForeignKey",
    "ModelContract",
    "PrimaryKey",
    "__version__",
    "contract",
    "models",
]
