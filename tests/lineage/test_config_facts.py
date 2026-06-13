"""Config discoverer: ``node.config`` keys become relation-scoped facts.

The first concrete mapping grounds candidate keys for the uniqueness property from
the ``unique_key`` / ``incremental_strategy`` pair. The semantics are load-bearing
and easy to get wrong, so these tests pin them at the boundary (the typed
``Manifest`` in, ``Fact[CandidateKeySet, SourceRef]`` out): a key is claimed only
when the materialization actually deduplicates on write, and the discoverer stays
silent everywhere else, because a wrong key is worse than a missing one.

The dedup semantics (dbt docs): ``merge`` dedups only with a ``unique_key`` and
behaves like ``append`` without one; ``delete+insert`` dedups and requires a key;
``append`` and ``insert_overwrite`` never dedup on the key. The default strategy
is adapter-dependent.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from dblect.lineage.facts.model import CompileOrigin, CompileValue
from dblect.lineage.graph import SourceKind, SourceRef
from dblect.lineage.properties.uniqueness import (
    CandidateKeySet,
    config_key_discoverer,
)
from dblect.manifest import (
    Manifest,
    ModelConfig,
    Node,
    ResourceType,
)


def _manifest(*nodes: Node, adapter_type: str = "duckdb") -> Manifest:
    return Manifest(
        schema_version="v12",
        adapter_type=adapter_type,
        nodes={n.unique_id: n for n in nodes},
    )


def _model(uid: str, *, config: ModelConfig | None = None) -> Node:
    return Node(
        unique_id=uid,
        name=uid.split(".")[-1],
        resource_type=ResourceType.MODEL,
        fqn=(uid,),
        package_name="shop",
        schema="analytics",
        raw_code=None,
        compiled_code="select 1",
        original_file_path=None,
        columns={},
        config=config,
    )


def _incremental(uid: str, *, strategy: str | None, unique_key: tuple[str, ...]) -> Node:
    return _model(
        uid,
        config=ModelConfig(
            materialized="incremental",
            incremental_strategy=strategy,
            unique_key=unique_key,
        ),
    )


def _keys(manifest: Manifest, *, adapter: str = "duckdb") -> list[CandidateKeySet]:
    facts = config_key_discoverer(adapter).discover(manifest, name_to_source={})
    return [f.value for f in facts]


def _key(*cols: str) -> frozenset[str]:
    return frozenset(cols)


# --- the pairs that DO enforce dedup -----------------------------------------


def test_merge_with_unique_key_grounds_the_key() -> None:
    model = _incremental("model.shop.events", strategy="merge", unique_key=("event_id",))
    facts = list(config_key_discoverer("duckdb").discover(_manifest(model), name_to_source={}))
    assert len(facts) == 1
    fact = facts[0]
    assert fact.scope == SourceRef(SourceKind.MODEL, model.unique_id)
    assert fact.value == CandidateKeySet.of(_key("event_id"))
    assert isinstance(fact.provenance, CompileValue)
    assert fact.provenance.origin is CompileOrigin.DBT_CONFIG


def test_delete_insert_with_unique_key_grounds_the_key() -> None:
    model = _incremental("model.shop.events", strategy="delete+insert", unique_key=("event_id",))
    assert _keys(_manifest(model)) == [CandidateKeySet.of(_key("event_id"))]


def test_composite_unique_key_grounds_a_composite_key() -> None:
    model = _incremental(
        "model.shop.events", strategy="merge", unique_key=("event_id", "event_date")
    )
    assert _keys(_manifest(model)) == [CandidateKeySet.of(_key("event_id", "event_date"))]


def test_unique_key_is_case_folded() -> None:
    model = _incremental("model.shop.events", strategy="merge", unique_key=("EventID",))
    assert _keys(_manifest(model)) == [CandidateKeySet.of(_key("eventid"))]


# --- the pairs that DO NOT enforce dedup (silence) ---------------------------


def test_append_with_unique_key_grounds_nothing() -> None:
    """``append`` never deduplicates, so a ``unique_key`` enforces nothing: claiming
    the key would be a wrong fact (silent duplicates the audit would then miss)."""
    model = _incremental("model.shop.events", strategy="append", unique_key=("event_id",))
    assert _keys(_manifest(model)) == []


def test_merge_without_unique_key_grounds_nothing() -> None:
    """``merge`` without a ``unique_key`` behaves like ``append``."""
    model = _incremental("model.shop.events", strategy="merge", unique_key=())
    assert _keys(_manifest(model)) == []


def test_insert_overwrite_grounds_nothing() -> None:
    """``insert_overwrite`` replaces partitions and ignores ``unique_key`` entirely."""
    model = _incremental("model.shop.events", strategy="insert_overwrite", unique_key=("event_id",))
    assert _keys(_manifest(model)) == []


def test_non_incremental_model_grounds_nothing() -> None:
    """A table/view materialization does not run incremental DML, so a stray
    ``unique_key`` carries no write-time guarantee."""
    model = _model(
        "model.shop.dim",
        config=ModelConfig(materialized="table", unique_key=("id",)),
    )
    assert _keys(_manifest(model)) == []


def test_model_without_config_grounds_nothing() -> None:
    assert _keys(_manifest(_model("model.shop.m"))) == []


# --- adapter-dependent default strategy --------------------------------------


def test_snowflake_default_strategy_is_merge() -> None:
    """With ``incremental_strategy`` unset, Snowflake defaults to ``merge``, so a
    ``unique_key`` does dedup and grounds a key."""
    model = _incremental("model.shop.events", strategy=None, unique_key=("event_id",))
    assert _keys(_manifest(model, adapter_type="snowflake"), adapter="snowflake") == [
        CandidateKeySet.of(_key("event_id"))
    ]


def test_spark_default_strategy_is_append() -> None:
    """Spark defaults to ``append``, which does not dedup, so the same model grounds
    nothing: the dedup guarantee genuinely differs by adapter."""
    model = _incremental("model.shop.events", strategy=None, unique_key=("event_id",))
    assert _keys(_manifest(model, adapter_type="spark"), adapter="spark") == []


def test_unknown_adapter_default_is_conservative() -> None:
    """When the adapter's default strategy is not known, an unset strategy claims
    no key rather than guessing: silence is the sound direction."""
    model = _incremental("model.shop.events", strategy=None, unique_key=("event_id",))
    assert _keys(_manifest(model, adapter_type="exotic"), adapter="exotic") == []


# --- only models carry incremental config ------------------------------------


def test_non_model_resource_grounds_nothing() -> None:
    """Incremental materialization is a model concept; a non-model node carrying a
    config-shaped value is not a relation this mapping addresses."""
    src = Node(
        unique_id="source.shop.raw.events",
        name="events",
        resource_type=ResourceType.SOURCE,
        fqn=("source.shop.raw.events",),
        package_name="shop",
        schema="raw",
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
        config=ModelConfig(
            materialized="incremental", incremental_strategy="merge", unique_key=("id",)
        ),
    )
    assert _keys(_manifest(src)) == []


# --- PBT: the fact mirrors the unique_key exactly when dedup is enforced ------

_cols = st.sampled_from(["id", "event_id", "event_date", "customer_id", "line_id"])
_dedup_strategy = st.sampled_from(["merge", "delete+insert"])
_no_dedup_strategy = st.sampled_from(["append", "insert_overwrite"])


@given(st.lists(_cols, min_size=1, max_size=3, unique=True), _dedup_strategy)
def test_dedup_strategy_grounds_exactly_the_unique_key(cols: list[str], strategy: str) -> None:
    model = _incremental("model.shop.m", strategy=strategy, unique_key=tuple(cols))
    keys = _keys(_manifest(model))
    assert keys == [CandidateKeySet.of(frozenset(c.lower() for c in cols))]
    assert keys[0] != CandidateKeySet.of()  # never a top-valued claim


@given(st.lists(_cols, min_size=1, max_size=3, unique=True), _no_dedup_strategy)
def test_no_dedup_strategy_grounds_nothing(cols: list[str], strategy: str) -> None:
    model = _incremental("model.shop.m", strategy=strategy, unique_key=tuple(cols))
    assert _keys(_manifest(model)) == []
