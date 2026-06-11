"""A fresh contract registry per test, so in-test ``ModelContract`` definitions do
not leak across the check-pipeline tests (mirrors ``tests/types/conftest``)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from dblect.types import ContractRegistry, isolated_registry


@pytest.fixture(autouse=True)
def registry() -> Iterator[ContractRegistry]:
    with isolated_registry() as reg:
        yield reg
