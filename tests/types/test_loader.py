"""Loading a project's ``dblect/`` declarations into a registry.

The loader imports every module under a project's declaration directory so each
``ModelContract`` registers, the import-time discovery the design relies on. Two
properties matter. It must not let the project's directory (also named ``dblect/``)
shadow the installed library, so the framework imports the modules use still
resolve. And one broken module must not blind the rest: an import failure becomes a
load issue on the report, not an exception that aborts the scan.
"""

from __future__ import annotations

from pathlib import Path

from dblect.loader import load_declarations


def _write(project: Path, rel: str, body: str) -> None:
    path = project / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def _project(tmp_path: Path) -> Path:
    decls = tmp_path / "dblect"
    _write(decls, "__init__.py", "")
    return tmp_path


def test_loads_contracts_into_a_registry(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write(
        project,
        "dblect/contracts/staging.py",
        "from dblect import ModelContract\n"
        "from dblect.demo import Money, Currency\n"
        "class StgPayments(ModelContract):\n"
        "    dbt_model = 'stg_payments'\n"
        "    amount: Money.refine(currency=Currency.USD)\n",
    )
    result = load_declarations(project)
    assert result.issues == ()
    assert [c.dbt_model for c in result.registry.contracts] == ["stg_payments"]


def test_relative_imports_within_the_declaration_package_resolve(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write(
        project,
        "dblect/types.py",
        "from dblect.demo import Money, Currency\nMoneyUSD = Money.refine(currency=Currency.USD)\n",
    )
    _write(
        project,
        "dblect/contracts/marts.py",
        "from dblect import ModelContract\n"
        "from ..types import MoneyUSD\n"
        "class FctOrders(ModelContract):\n"
        "    dbt_model = 'fct_orders'\n"
        "    order_total: MoneyUSD(amount='order_total')\n",
    )
    result = load_declarations(project)
    assert result.issues == ()
    assert {c.dbt_model for c in result.registry.contracts} == {"fct_orders"}


def test_a_broken_module_becomes_an_issue_not_a_crash(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write(
        project,
        "dblect/contracts/good.py",
        "from dblect import ModelContract\nclass Good(ModelContract):\n    dbt_model = 'good'\n",
    )
    _write(project, "dblect/contracts/broken.py", "import nonexistent_module_xyz\n")
    result = load_declarations(project)
    assert [c.dbt_model for c in result.registry.contracts] == ["good"]
    assert len(result.issues) == 1
    assert "broken" in result.issues[0].module


def test_stubs_directory_is_skipped(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write(project, "dblect/_stubs/models.py", "raise RuntimeError('stubs must not be imported')\n")
    _write(
        project,
        "dblect/contracts.py",
        "from dblect import ModelContract\nclass C(ModelContract):\n    dbt_model = 'c'\n",
    )
    result = load_declarations(project)
    assert result.issues == ()
    assert [c.dbt_model for c in result.registry.contracts] == ["c"]


def test_missing_declaration_dir_loads_nothing(tmp_path: Path) -> None:
    result = load_declarations(tmp_path)
    assert result.registry.contracts == ()
    assert result.issues == ()
