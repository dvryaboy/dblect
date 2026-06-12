"""Load a project's ``dblect/`` declarations into a registry.

Defining a ``ModelContract`` registers it, so loading is just importing every
module under the project's declaration directory. Two wrinkles make this more than
``importlib.import_module``. The directory is itself named ``dblect/``, which would
shadow the installed library if imported as a top-level package, so it is imported
under a unique synthetic package name; the framework imports the modules use
(``from dblect import ModelContract``) keep resolving to the real library, and the
project's own relative imports (``from ..types import ...``) resolve within the
synthetic package. And one broken module must not abort the scan, so an import
failure is recorded as a :class:`LoadIssue` and the remaining modules still load.
The whole import runs inside a fresh registry, returned for the bridge to resolve.

See ``docs/design/dblect_technical_intro.md`` (loading lifecycle).
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from dblect.types import ContractRegistry, isolated_registry

_STUBS_DIR = "_stubs"


@dataclass(frozen=True, slots=True)
class LoadIssue:
    """One module the loader could not import, with the failure's text."""

    module: str
    message: str


@dataclass(frozen=True, slots=True)
class LoadResult:
    """The registry a load populated, plus the modules that failed to import."""

    registry: ContractRegistry
    issues: tuple[LoadIssue, ...]


def load_declarations(project_dir: Path, *, decl_dir_name: str = "dblect") -> LoadResult:
    """Import every module under ``<project_dir>/<decl_dir_name>/`` into a fresh
    registry. A missing directory loads nothing (a zero-declaration project is
    valid). The ``_stubs`` directory is skipped: it is generated output, imported
    only by the contracts that reference it, never scanned."""
    decl_root = project_dir / decl_dir_name
    if not decl_root.is_dir():
        with isolated_registry() as reg:
            return LoadResult(reg, ())

    pkg_name = f"_dblect_declarations_{uuid.uuid4().hex}"
    issues: list[LoadIssue] = []
    with isolated_registry() as reg:
        try:
            # A broken root __init__ aborts this load (nothing under it could resolve
            # its relative imports anyway), but as an issue, not an escaping exception.
            with _synthetic_package(pkg_name, decl_root):
                for dotted in _module_names(decl_root):
                    full = f"{pkg_name}.{dotted}" if dotted else pkg_name
                    try:
                        importlib.import_module(full)
                    except Exception as exc:
                        issues.append(LoadIssue(module=dotted or decl_dir_name, message=str(exc)))
        except Exception as exc:
            issues.append(LoadIssue(module=decl_dir_name, message=str(exc)))
    return LoadResult(reg, tuple(issues))


def _module_names(decl_root: Path) -> list[str]:
    """The dotted module names under ``decl_root`` (relative to it), parents before
    children, skipping the package root ``__init__`` and the ``_stubs`` tree."""
    names: list[str] = []
    for path in sorted(decl_root.rglob("*.py")):
        rel = path.relative_to(decl_root)
        if _STUBS_DIR in rel.parts:
            continue
        parts = list(rel.with_suffix("").parts)
        if parts == ["__init__"]:
            continue  # the package root itself, loaded when the synthetic package is created
        if parts[-1] == "__init__":
            parts = parts[:-1]  # a subpackage: its dotted name is the directory
        names.append(".".join(parts))
    # Shorter dotted names (packages) before longer (their modules).
    return sorted(names, key=lambda n: (n.count("."), n))


@dataclass
class _synthetic_package:  # noqa: N801 (used as a context manager)
    """Register ``decl_root`` as an importable package under ``pkg_name`` for the
    duration of the block, then evict it and its submodules from ``sys.modules`` so
    repeated loads stay independent."""

    pkg_name: str
    decl_root: Path

    def __enter__(self) -> ModuleType:
        init_file = self.decl_root / "__init__.py"
        locations = [str(self.decl_root)]
        if init_file.exists():
            spec = importlib.util.spec_from_file_location(
                self.pkg_name, init_file, submodule_search_locations=locations
            )
            assert spec is not None
            assert spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[self.pkg_name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                # __exit__ does not run when __enter__ raises, so a failed root
                # import would otherwise leave the package wedged in sys.modules.
                del sys.modules[self.pkg_name]
                raise
        else:
            module = ModuleType(self.pkg_name)
            module.__path__ = locations
            sys.modules[self.pkg_name] = module
        return module

    def __exit__(self, *_: object) -> None:
        prefix = f"{self.pkg_name}."
        for name in [n for n in sys.modules if n == self.pkg_name or n.startswith(prefix)]:
            del sys.modules[name]
