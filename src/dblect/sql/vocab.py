"""SQL-grammar vocabulary shared across the analysis layers.

These are dialect-independent ``exp.*`` facts (the parser picks the concrete
class per dialect, but the class itself is not dialect-specific), so they live in
the ``sql`` layer rather than inside whichever property consumes them. The
uniqueness property reads the surrogate-hash grammar to recognise a hash of a
structural column combination as a key.
"""

from __future__ import annotations

import sqlglot.expressions as exp
from sqlglot import Expr

# --- surrogate-hash grammar --------------------------------------------------
#
# The typed-node vocabulary for recognizing a surrogate-hash key: a hash of a
# structural combination of columns. An adapter that hashes via a function
# sqlglot parses to `exp.Anonymous` would compose a name set on top, as the
# non-determinism builtins do; nothing demands that yet.
#
# These are tuples, not frozensets, because membership is tested with
# `isinstance`, whose subclass-awareness is load-bearing: `TO_HEX(...)` parses to
# `exp.LowerHex`, a subclass of `exp.Hex`, so listing `Hex` looks through the hex
# wrapper. A hash's hex and raw-digest spellings, though, are siblings, not in a
# subclass relation (`MD5`/`MD5Digest`, `SHA2`/`SHA2Digest`), so both are listed
# explicitly. Resolved by name for tolerance across sqlglot versions.
SURROGATE_HASH_FUNCTIONS: tuple[type[Expr], ...] = tuple(
    getattr(exp, n)
    for n in ("MD5", "MD5Digest", "SHA", "SHA1Digest", "SHA2", "SHA2Digest", "FarmFingerprint")
    if hasattr(exp, n)
)
# Single-argument wrappers that do not change which tuple is hashed, looked through
# to reach the hash (e.g. `TO_HEX(MD5(...))`, `LOWER(...)`).
SURROGATE_HASH_PASSTHROUGH: tuple[type[Expr], ...] = tuple(
    getattr(exp, n) for n in ("Hex", "Lower", "Upper") if hasattr(exp, n)
)
# Structural combinators that assemble columns into the hashed value without making
# the input anything other than those columns.
SURROGATE_HASH_STRUCTURAL: tuple[type[Expr], ...] = tuple(
    getattr(exp, n)
    for n in ("Concat", "DPipe", "Cast", "TryCast", "Coalesce", "Lower", "Upper", "Trim", "Paren")
    if hasattr(exp, n)
)
