"""Guard against sqlglot's compiled build, which dblect cannot use.

``sqlglotc`` (pulled by ``sqlglot[c]``) is a mypyc-compiled build of sqlglot: it
overlays compiled ``.so`` modules that Python loads in preference to the ``.py``
sources, and sqlglot activates it automatically whenever the package is importable.
dblect extends sqlglot by subclassing its ``Expression`` (the synthetic lineage
nodes ``UnionConfluence`` and ``OuterJoinNull``) and its ``MappingSchema`` (a
column-type memo). mypyc lets an interpreted class *define* a subclass of a compiled
one but rejects *instantiating* it, so under the compiled build those constructions
raise ``TypeError: interpreted classes cannot inherit from compiled`` mid-analysis,
and some sites degrade to dropped models (silently missing findings) instead.

Full compatibility would mean rearchitecting those node types, and the compiled
tokenizer buys no measurable speedup at dblect's scale, so the supported posture is
pure-Python sqlglot. This module detects the incompatible build and lets callers fail
fast with an actionable message rather than crash cryptically or under-report.
"""

from __future__ import annotations

import sqlglot.expressions as exp


class SqlglotCompatibilityError(RuntimeError):
    """The active sqlglot is the compiled build, which dblect cannot run against."""


def sqlglot_supports_subclassing() -> bool:
    """Whether an interpreted subclass of a sqlglot class can be instantiated.

    Exercises the exact capability dblect depends on: it constructs a throwaway
    subclass of ``exp.Expression`` and instantiates it. Pure-Python sqlglot allows
    this; the mypyc-compiled build raises ``TypeError`` at instantiation. The base
    ``Expression`` takes no required constructor arguments, so a failure here is the
    compiled-inheritance rejection rather than an argument mismatch.
    """
    try:
        type("_probe", (exp.Expression,), {})()  # pyright: ignore[reportPrivateImportUsage]
    except TypeError:
        return False
    return True


def ensure_pure_sqlglot() -> None:
    """Raise :class:`SqlglotCompatibilityError` when the compiled sqlglot is active.

    A no-op on pure-Python sqlglot. Callers run this at an analysis entry point so a
    compiled build present in the environment surfaces as one clear, self-service
    error instead of a ``TypeError`` from deep in the lineage builder.
    """
    if sqlglot_supports_subclassing():
        return
    raise SqlglotCompatibilityError(
        "dblect cannot run against sqlglot's compiled build (sqlglotc, installed by "
        "the sqlglot[c] extra): dblect subclasses sqlglot's Expression and "
        "MappingSchema, which the mypyc-compiled build forbids. Install the "
        "pure-Python sqlglot instead, e.g. `pip uninstall sqlglotc` (or reinstall "
        "sqlglot without the [c] extra)."
    )
