# Var inference: the Jinja front end

Status: implemented in [#104](https://github.com/dvryaboy/dblect/pull/104), split off `main` ahead of the rest of this stream
Audience: engineers implementing the source-Jinja walker for var discovery
Part of: [the var-inference plan](./plan.md)

The parsing environment (`src/dblect/varinf/environment.py`), the context-carrying walker (`walker.py`), and the `VarUsage` / `UsageContext` contracts (`usage.py`) land in #104, with the rule-by-rule pins and two property tests (discovery completeness against an independent `find_all` oracle, and totality on arbitrary input) in `tests/varinf/`. Two boundaries are deferred and noted under [Open questions](#open-questions): `is_incremental()` and the `SqlLiteral` position hint.

This stream turns a node's source Jinja into a structured record of every `var()` and `env_var()` reference it makes directly (macro indirection is the [macro-following](./macro-following.md) stream). It is the second of dblect's two front ends: sqlglot over compiled SQL for structure, and this Jinja AST over source for the variability compiled SQL has already erased.

## Why a Jinja AST and not the compiled SQL

By the time dbt has run, a value-substitution var has collapsed to a literal indistinguishable from a hand-typed constant, and a control-flow var has had one branch chosen and the other erased. The compiled SQL the rest of dblect analyzes (`Node.analysis_sql`, which returns `compiled_code`) cannot see either. The on-disk template (`Node.raw_code`) still carries both, so this stream reads `raw_code` and parses it.

## What the parser gives us

A probe of `jinja2.Environment().parse()` established that the parser yields a clean AST in which every `UsageContext` the spec needs maps directly to a node shape, with literal operands resolved inline. The mapping the walker relies on:

| Spec `UsageContext` | AST shape |
|---|---|
| `TruthyTest` | the `var` `Call` is the `If.test` (or nested under boolean ops in it) |
| `Equality(operand)` | `Compare` whose first operand is the `var` `Call`, with an `eq` `Operand(Const)` |
| `Inequality(operand, op)` | `Compare` with a `lt` / `gt` / `lteq` / `gteq` `Operand(Const)` |
| `InSet(elements)` | `Compare` with an `in` `Operand(List[Const])` |
| `Arithmetic(op, other)` | `Mul` / `Add` / etc. wrapping the `var` `Call` |
| `SqlLiteral(position)` | the `var` `Call` sits under an `Output` node (interpolation) |
| `MacroArg(macro, position)` | the `var` `Call` is an argument of another `Call` whose name is not `var` / `env_var` |
| inline `var(name, default)` | the `Call` carries a second `Const` argument |

`env_var` versus `var` is the callee name on the `Call`. `config()`, `ref()`, `source()`, `{{ this }}`, whitespace control, `{% set %}`, and boolean `and` / `or` all parse cleanly and are simply not `var` / `env_var` calls, so the walker ignores them.

`is_incremental()` is also a `Call` to a bare name, and the walker sees it the same way, but it does not emit a `VarUsage` for it: it is not a var, it is an always-present control-flow axis whose type (`bool`), domain (first-run versus steady-state), and trigger (incremental materialization) are all known a priori. It enters the world system through bounded re-compilation keyed on `materialized == 'incremental'`, not through discovery, so forcing it into the var-shaped `VarUsage` would muddy that contract. If the per-model responsiveness map ever needs to know which models branch on it, that is a separate control-flow marker, not a `VarUsage`. The same holds for `target.name`.

The control-flow versus value-substitution signal, which the [classification](./inference-and-classification.md) stream and #99 / #100 depend on, is recoverable from the AST alone: a `var` `Call` reached under an `If.test`, a `For.iter`, or a branch-steering `Compare` is control-flow; one reached only under an `Output` or a plain expression is value-substitution. No text heuristics are needed for that decision.

One footgun the C product-line literature warns about (TypeChef's token-straddling, where `{{ var('schema') }}.users` crosses a SQL token boundary) does not bite this stream. We parse the Jinja, not the rendered SQL, and that snippet parses cleanly into `Output` plus `TemplateData`. Token-straddling is a concern for the variability-aware compilation endgame in #100, where the rendered SQL is re-parsed, not for var discovery.

## The parsing environment

A bare `jinja2.Environment()` rejects tags dbt relies on. A probe of the fixture's macro bodies found the failures fall into a small, known set, and two of the categories are standard Jinja extensions rather than dbt inventions:

| Tag | Source | Treatment |
|---|---|---|
| `do` | `jinja2.ext.do` (stdlib) | enable the extension |
| `continue` / `break` | `jinja2.ext.loopcontrols` (stdlib) | enable the extension |
| `materialization` | dbt block tag | generic block-tag extension |
| `snapshot` | dbt block tag | generic block-tag extension |
| `test` | dbt block tag (legacy generic-test definition) | generic block-tag extension |
| `docs` | dbt block tag | generic block-tag extension |

The dbt block tags share one shape, `{% TAG ...header... %} body {% endTAG %}`, handled by a single extension that skips the header tokens and parses the body as statements so a `var()` inside survives with its context intact:

```python
class DbtBlockTags(Extension):
    tags = {"materialization", "snapshot", "docs", "test"}

    def parse(self, parser):
        tag = parser.stream.current.value
        lineno = next(parser.stream).lineno
        while parser.stream.current.type != "block_end":  # skip the header tokens
            next(parser.stream)
        body = parser.parse_statements((f"name:end{tag}",), drop_needle=True)
        return nodes.Scope(body, lineno=lineno)            # parse INTO the body
```

The load-bearing choice is `parse_statements` over skip-to-end: it parses the body as real statements, so a `var()` inside a snapshot, including one nested in an `{% if %}`, keeps its syntactic context and is correctly classified as control-flow. We skip only the header tokens (`snapshot name`, `materialization ..., adapter='x'`), where vars do not live. The probe confirmed the fixture's macro bodies parse cleanly under this environment and that a `var()` inside a snapshot body is reached with its control-flow context preserved.

The tag set is dbt's documented vocabulary, so it is closed and extending it is a one-line change. This makes snapshots first-class for var discovery: their `.sql` body is read through the same path, and the manifest supplies snapshot config (the validity columns already modeled in `ModelConfig`) separately.

## Degrade-not-lie on parse failure

Anything the environment still cannot parse (an exotic custom extension, a malformed body) must become one honest diagnostic, never a crash and never a silent miss. The walker catches the parse failure, records the node (or macro) as opaque with the reason, and the var it would have carried degrades to a single resolved world downstream. This is the spec's "detect by parse failure, mark opaque" rule, and it is the backstop that keeps the closed tag set from being a soundness risk: a tag we have not enumerated costs coverage, not correctness.

## Outputs

The walker emits `VarUsage` records as specified in [`var-inference-spec.md`](../var-inference-spec.md): var name, kind (`var` / `env_var`), `UsageContext`, source location (file, line, column from the AST node's `lineno` and the node's `original_file_path`), the macro trail (empty for direct usage, filled by the [macro-following](./macro-following.md) stream), and a confidence marker. Source location comes from the Jinja node's line number plus the node's file path; the column is best-effort since jinja2 nodes carry line but not column.

## Testing

- A test that every macro body in the fixture set parses under the configured environment, so a new dbt tag surfaces as a failing test rather than silent opaque-ing.
- Per-rule unit tests: one synthetic template per `UsageContext`, asserting the emitted `VarUsage` (kind, context, operand). These are the rule-by-rule pins the spec's testing strategy calls for.
- A snapshot-body test asserting a `var()` inside `{% snapshot %}` (and inside a nested `{% if %}` within it) is discovered with control-flow context.
- A parse-failure test asserting an unparseable body yields an opaque diagnostic rather than raising.

## Open questions

- **`SqlLiteral` position hint.** The spec's `SqlLiteral(position)` distinguishes quoted-string, numeric, and identifier positions, which type inference reads (a numeric `LIMIT` position infers numeric, a quoted position infers `str`). Deciding that needs the SQL context around the interpolation, which the Jinja AST does not carry: an `Output` node knows it interpolates a value, not what SQL token surrounds it. #104 emits `SqlLiteral(UNKNOWN)` and defers the refinement (recovering the surrounding text, or a light re-lex) rather than guessing. Until then a value-substitution var still types from its other usages and from its declared default.
- **Column position in source locations.** jinja2 AST nodes carry `lineno` but not column. Whether to recover column by re-lexing or to accept line-only locations in v1 (#104 takes line-only).
- **`is_incremental()` and `target` as control-flow markers.** The walker recognizes these but emits no `VarUsage` (they are not vars). They are handled as always-present axes by bounded re-compilation. Whether the front end should additionally emit a non-var control-flow marker per model to feed per-model responsiveness ([#99](https://github.com/dvryaboy/dblect/issues/99)) is deferred to the always-present-axis stream that owns them.
- **Vars in seeds and sources.** The spec defers these. They have no `raw_code` to walk, so they are out of this stream's reach regardless; if they are wanted, they enter through config rather than the Jinja walk.

## References

- [The var-inference plan](./plan.md) and [the original spec](../var-inference-spec.md).
- The two-front-end framing: [`config-and-flag-worlds.md`](../config-and-flag-worlds.md), "The parsing reality".
- The downstream consumers of the control-flow signal: [inference and classification](./inference-and-classification.md), [#99](https://github.com/dvryaboy/dblect/issues/99), [#100](https://github.com/dvryaboy/dblect/issues/100).
</content>
