"""Shared fixtures for the declaration-layer tests.

Every test runs against a fresh contract registry so module import order and
test order cannot leak `ModelContract` registrations across tests. Contract
classes are therefore defined inside test functions; `DomainType` definitions
carry no registration side effect and may live at module level.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from dblect.types import ContractRegistry, isolated_registry


@pytest.fixture(autouse=True)
def registry() -> Iterator[ContractRegistry]:
    with isolated_registry() as reg:
        yield reg
