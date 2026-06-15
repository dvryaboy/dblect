"""The adapter registry: where target profiles are registered and looked up.

Supporting a new warehouse is self-contained. Drop a module under
:mod:`dblect.adapters.builtin` (or, out of tree, any module a host imports) that
builds an :class:`AdapterProfile` and calls :func:`register`. The built-in
modules are auto-discovered on first lookup, so nothing here enumerates them and
adding one edits no shared file.

A dbt adapter name and a sqlglot dialect name are two namespaces that overlap by
name without being the same thing (a custom adapter may share Snowflake's SQL
grammar, for instance). The profile carries both, and :func:`resolve_profile` is
the one place a run commits to a target: an explicit override names that target
wholesale, so its grammar and its runtime semantics always agree.
"""

from __future__ import annotations

import importlib
import pkgutil

from dblect.adapters.model import AdapterProfile

_PROFILES: dict[str, AdapterProfile] = {}
_loaded = False


def register(profile: AdapterProfile) -> AdapterProfile:
    """Register ``profile`` under its (case-folded) adapter name and return it, so a
    module can both register and keep a reference: ``SNOWFLAKE = register(...)``.

    A later registration of the same name wins, which lets a host refine or replace
    a built-in profile without editing it.
    """
    _PROFILES[profile.adapter_type.strip().lower()] = profile
    return profile


def _ensure_loaded() -> None:
    """Import the built-in adapter modules once, so their ``register`` calls run.

    Discovery walks the :mod:`dblect.adapters.builtin` package rather than naming
    its modules, so a new built-in is picked up without being listed here. The flag
    is set before importing so a module that triggers a lookup during its own
    import does not recurse.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True
    from dblect.adapters import builtin

    for info in pkgutil.iter_modules(builtin.__path__, builtin.__name__ + "."):
        importlib.import_module(info.name)


def _conservative(adapter_type: str, *, sqlglot_dialect: str | None = None) -> AdapterProfile:
    """The profile for an adapter dblect has no specific knowledge of: NOT NULL
    enforced (true on essentially every warehouse), PRIMARY KEY / UNIQUE advisory,
    and no known dedup default, so an unset incremental strategy claims no key."""
    return AdapterProfile(
        adapter_type=adapter_type,
        sqlglot_dialect=sqlglot_dialect if sqlglot_dialect is not None else adapter_type,
        validated=False,
        not_null_enforced=True,
        key_enforced=False,
        default_incremental_strategy=None,
    )


def profile_for_adapter(adapter_type: str) -> AdapterProfile:
    """The capability profile for a dbt adapter by name.

    An adapter no module has registered gets a conservative profile, never an
    error: this is the semantics lookup, distinct from the parsing-validation gate
    in :func:`resolve_profile`.
    """
    _ensure_loaded()
    return _PROFILES.get(adapter_type.strip().lower()) or _conservative(adapter_type)


def validated_adapters() -> frozenset[str]:
    """The names of registered adapters dblect has validated end-to-end."""
    _ensure_loaded()
    return frozenset(name for name, profile in _PROFILES.items() if profile.validated)


class UnvalidatedAdapterError(ValueError):
    """The manifest's adapter is not in dblect's validated set and no ``--dialect``
    override is in effect. Carries the adapter name so the CLI can build an
    actionable message."""

    def __init__(self, adapter_type: str) -> None:
        super().__init__(
            f"adapter `{adapter_type}` is not in dblect's validated set "
            f"({sorted(validated_adapters())})"
        )
        self.adapter_type = adapter_type


def resolve_profile(*, adapter_type: str, explicit_dialect: str | None) -> AdapterProfile:
    """The single target profile for a run, or raise :class:`UnvalidatedAdapterError`.

    An ``explicit_dialect`` override names the target wholesale (its grammar and its
    runtime semantics together), so the two cannot drift apart; passing it is the
    operator's acknowledgment of a best-effort, possibly unvalidated
    interpretation. Without an override the manifest's adapter must be validated.
    """
    if explicit_dialect is not None:
        return profile_for_adapter(explicit_dialect)
    profile = profile_for_adapter(adapter_type)
    if not profile.validated:
        raise UnvalidatedAdapterError(adapter_type)
    return profile
