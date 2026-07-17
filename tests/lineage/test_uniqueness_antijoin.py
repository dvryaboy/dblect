"""Candidate keys across an anti-join.

An anti-join keeps a probe-side row only when it has no match on the other side, so it is
row-removing and never multiplies the probe. The probe relation's candidate keys therefore
carry through unchanged, regardless of whether the matched relation has a key of its own.
That is the contract these tests pin, for every surface form the shared classifier recognises
(native ``ANTI`` / ``SEMI``, and the ``LEFT JOIN ... IS NULL`` idiom), against a *keyless*
matched relation so the preservation cannot be borrowed from the matched side.

The contrast tests are what give it teeth: the same shape as a plain ``LEFT`` / ``INNER`` join
to the keyless relation drops the probe key (that join can fan out), so a passing anti-join
case is proving the row-removing semantics rather than some unrelated preservation.
"""

from __future__ import annotations

from dblect.adapters import profile_for_adapter
from dblect.lineage.builder import build_relation_graph
from dblect.lineage.properties.uniqueness import Key, uniqueness_property
from dblect.lineage.property import propagate
from dblect.manifest import DbtTestMetadata, Manifest, Node, ResourceType

_DUCKDB = profile_for_adapter("duckdb")

_L = "source.test.raw.l"
_R = "source.test.raw.r"
_MODEL = "model.test.probe"


def _source(unique_id: str, name: str) -> Node:
    return Node(
        unique_id=unique_id,
        name=name,
        resource_type=ResourceType.SOURCE,
        fqn=("test", name),
        package_name="test",
        schema="raw",
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
    )


def _unique(unique_id: str, *, column: str, target: str) -> Node:
    return Node(
        unique_id=unique_id,
        name=unique_id.split(".")[-1],
        resource_type=ResourceType.OTHER,
        fqn=(unique_id,),
        package_name="test",
        schema=None,
        raw_code=None,
        compiled_code=None,
        original_file_path=None,
        columns={},
        depends_on=frozenset({target}),
        test_metadata=DbtTestMetadata(name="unique", kwargs={"column_name": column}),
        attached_node=target,
    )


def _model(sql: str) -> Node:
    return Node(
        unique_id=_MODEL,
        name="probe",
        resource_type=ResourceType.MODEL,
        fqn=("test", "probe"),
        package_name="test",
        schema="analytics",
        raw_code=sql,
        compiled_code=sql,
        original_file_path=None,
        columns={},
        depends_on=frozenset({_L, _R}),
    )


def _keys(sql: str, *, l_unique_on: str) -> frozenset[Key]:
    """The model's inferred keys, with ``l`` declared unique on ``l_unique_on`` and ``r`` keyless.
    Any surviving key is one the anti-join preserved from the probe side ``l``."""
    nodes = [
        _source(_L, "l"),
        _unique("test.l_pk", column=l_unique_on, target=_L),
        _source(_R, "r"),
        _model(sql),
    ]
    manifest = Manifest(
        schema_version="v12", adapter_type="duckdb", nodes={n.unique_id: n for n in nodes}
    )
    anns = propagate(build_relation_graph(manifest).graph, uniqueness_property(manifest, _DUCKDB))
    return next(ann.value.keys for ref, ann in anns.items() if ref.unique_id == _MODEL)


def _key(*cols: str) -> Key:
    return frozenset(cols)


# --- an anti-join preserves the probe's keys, matched side keyless ----------------

_ANTI = "SELECT l.id, l.k FROM l ANTI JOIN r ON l.k = r.k"
_SEMI = "SELECT l.id, l.k FROM l SEMI JOIN r ON l.k = r.k"
_LEFT_IS_NULL = "SELECT l.id, l.k FROM l LEFT JOIN r ON l.k = r.k WHERE r.k IS NULL"


def test_native_anti_join_preserves_the_probe_key() -> None:
    """``l`` is unique on ``id``, a column the anti-join does not even touch: the row-removing
    filter keeps that key though ``r`` has none of its own."""
    assert _keys(_ANTI, l_unique_on="id") == {_key("id")}


def test_semi_join_preserves_the_probe_key() -> None:
    """SEMI filters the probe rows just as ANTI does, so the probe key rides through the same way."""
    assert _keys(_SEMI, l_unique_on="id") == {_key("id")}


def test_left_join_is_null_preserves_the_probe_key() -> None:
    """The ``LEFT JOIN ... WHERE r.k IS NULL`` idiom keeps only the unmatched probe rows, one per
    probe row, so the probe key survives even though the plain LEFT join to keyless ``r`` could
    fan out."""
    assert _keys(_LEFT_IS_NULL, l_unique_on="id") == {_key("id")}


def test_anti_join_preserves_a_key_that_is_the_join_column() -> None:
    """Preservation is unconditional in the probe's keys, so a key that *is* the join column
    carries through as readily as one that is not."""
    assert _keys(_ANTI, l_unique_on="k") == {_key("k")}


# --- contrast: the same shapes as ordinary joins drop the key ---------------------

_LEFT_PLAIN = "SELECT l.id, l.k FROM l LEFT JOIN r ON l.k = r.k"
_INNER_PLAIN = "SELECT l.id, l.k FROM l JOIN r ON l.k = r.k"


def test_plain_left_join_to_keyless_relation_drops_the_key() -> None:
    """Without the ``IS NULL`` filter the LEFT join can match a probe row to many keyless ``r``
    rows and fan out, so the probe key does not survive. This is the anti-join's foil: the filter
    is exactly what rescues the key."""
    assert _keys(_LEFT_PLAIN, l_unique_on="id") == frozenset()


def test_plain_inner_join_to_keyless_relation_drops_the_key() -> None:
    """The baseline that a keyless matched relation normally kills the probe key: an INNER join to
    keyless ``r`` can multiply probe rows."""
    assert _keys(_INNER_PLAIN, l_unique_on="id") == frozenset()
