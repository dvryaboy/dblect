# Code Style
We avoid stringy typing like the plague.
We use rigorous types.

We reach for an existing helper before writing a parallel one, and generalize it when a second caller needs a variant. A comment or commit message that reaches for "mirroring", "duplicate of", or "acceptable for now" is a signal to stop and reuse the original instead.

## Testing
We test rigorously. 
Whenever applicable, we design quality PBT tests, or otherwise leverage provers and exhaustive testing.
We do not write tests for tests' sakes. We avoid test theater. 
We prefer to test at the boundaries instead of implementation specifics; tests should survive implementation
changes that do not change contracts.
Excessive use of mocking is a smell. Loads of boilerplate is a smell.
Pin contracts, not coincidences. If fragile cooperation is observed (two functions work correctly together only because of implementation details, not contract guarantees), fix the contracts in code rather than pinning the accidental behavior in a test.

We work test-first: the failing test (or the property / lattice-law spec) comes before the implementation, so it pins the intended contract rather than rationalizing whatever the code happens to do. When the contract is amenable, we design the property-based test and its generators first, before the code, so the generator is not shaped to fit the implementation.

A whole suite passing on its first run is a smell rather than a win: it usually means the tests are weak or were written after the code. When it happens, we prove the tests bite by injecting deliberate contract violations and confirming a test fails for each.

We enumerate the input space rather than sampling it: a condition over a closed type earns a test per value, and the common ones (`COUNT(*)` among the duplicate-sensitive aggregates) belong in that space rather than being deferred as edge cases.

## Soundness
We discharge each predicate or guard to an exact decision procedure before coding it, with a counterexample that marks its edge and a test that pins it. A plausible structural proxy standing in for a precise semantic condition is where the subtle bugs live.

A soundness check that branches on a closed type decides every case explicitly, rather than handling one (a `CROSS` join) and letting the rest fall through to the wrong answer.

When new code consumes an existing fact or invariant, we re-establish that fact's soundness across its full input space, even when we did not write it. A new consumer can raise the stakes on a latent over-claim and turn it into a wrong answer.

## Comments
We avoid comment bloat. We write comments that explain the why of things, or help understand particularly
complex bits of code and call attention to footguns.

Specific things to keep out of comments, docstrings, and prose:
- Phase or tier numbering ("Tier 0", "phase 1", etc.). The numbering is arbitrary and reorderable; refer to work streams by name (e.g., "static analyser", "replay-determinism check", "runtime PBT loop").
- Narrative counts of fixture or repo contents ("5 models + 3 seeds + 20 tests"). They go stale silently; if a number matters, assert it.
- References to ephemeral planning files such as `HANDOFF.md`. Those are session-to-session notes and should not be cited from code, tests, or docs.

## Prose Style
All narrative documents and comments should avoid em-dashes and "not x. not y. z" phrasing.
We do not bash older or alternative approaches: we win by being clear and positive, and complimenting them / acknowledging good ideas and influences when appropriate. We are generous with compliments and praise. We put our work in the context of the broader field. 

# Development
We run Python and its tooling through `uv` (`uv run python`, `uv run pytest`, `uv run ruff`, `uv run pyright`); a bare `python` is not on PATH. Quick probes go through `uv run python -c "..."` rather than a throwaway file.

Before pushing we run the full gate, which CI runs as separate steps: `uv run ruff check`, `uv run ruff format --check`, `uv run pyright`, `uv run pytest`.

