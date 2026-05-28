# Code Style
We avoid stringy typing like the plague.
We use rigorous types.

## Testing
We test rigorously. 
Whenever applicable, we design quality PBT tests, or otherwise leverage provers and exhaustive testing.
We do not write tests for tests' sakes. We avoid test theater. 
We prefer to test at the boundaries instead of implementation specifics; tests should survive implementation
changes that do not change contracts.
Excessive use of mocking is a smell. Loads of boilerplate is a smell.
Pin contracts, not coincidences. If fragile cooperation is observed (two functions work correctly together only because of implementation details, not contract guarantees), fix the contracts in code rather than pinning the accidental behavior in a test.

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

