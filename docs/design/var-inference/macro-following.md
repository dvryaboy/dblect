# Var inference: macro following

Status: design
Audience: engineers implementing the macro expansion engine
Part of: [the var-inference plan](./plan.md)

Many `var()` calls are reached only through macros: a model calls `{{ get_flag('include_tax') }}`, and the `var()` lives inside `get_flag`'s body. This stream follows those calls. It is separable from direct-usage discovery in the [front end](./jinja-frontend.md) and can land after it; a project whose vars are all referenced directly is fully handled without this stream, which covers the majority of small projects.

The engine walks over already-parsed ASTs. The [front end](./jinja-frontend.md) configures the parsing environment and the [discovery-inputs](./discovery-inputs.md) stream supplies the macro registry (implemented in [#103](https://github.com/dvryaboy/dblect/pull/103): `Manifest.macros`, keyed by `unique_id`), so this stream's job is the expansion and recursion logic, not parsing.

### Inherited from discovery-inputs: name resolution

The registry is keyed by `unique_id`, but a call site names a macro by bare name or `package.name`. discovery-inputs deferred the name-to-definition resolver here on purpose, since its contract is fixed by how this walker dispatches rather than by the manifest shape. This stream implements it: a resolver over `Manifest.macros` that applies dbt's package-qualification rule (a bare name resolves within the project first, then packages; a `package.name` reference resolves directly), returning the `Macro` or signalling no match (an opaque usage, not an error). The registry already carries `name` and `package_name` on every entry, so this is pure lookup logic with no new manifest reading. Its test (the bare-name-then-package order and the `package.name` direct path) lands with it here.

## The walk

When the AST walk encounters a `Call` whose callee names a macro in the registry (rather than `var` / `env_var`), the engine:

1. Resolves the callee name to a registry entry through the name resolver above (bare-name-then-package, or `package.name` direct).
2. Takes the macro's already-parsed body AST.
3. Substitutes the call-site arguments for the macro's parameters (lexical substitution, described below).
4. Walks into the substituted body, continuing to collect `VarUsage` records, with the macro name pushed onto the trail.
5. Pops the macro from the trail when the sub-walk completes.

The walk recurses when a macro body itself calls macros. Each `VarUsage` collected through a macro carries the trail of macros traversed to reach it, which the diagnostic report surfaces so the user can see where an inferred var came from.

## Lexical parameter substitution

A macro call binds positional and keyword arguments to the macro's declared parameters. Substitution replaces each parameter `Name` in the body AST with the argument expression from the call site. When the argument is a literal, the parameter becomes that `Const`, which is what makes symbolic conditional evaluation (below) possible. When the argument is itself a `var()` call or a more complex expression, the parameter becomes that subtree, so a `var()` passed as a macro argument is followed into the position the parameter occupies.

The substitution is lexical, not a full Jinja evaluation: it rewrites the AST, it does not render. This keeps the engine static and free of a live dbt at analysis time.

## Internal control flow

A macro body can branch on a parameter:

```jinja
{% macro tricky(condition) %}
  {% if condition %}{{ var('option_a') }}{% else %}{{ var('option_b') }}{% endif %}
{% endmacro %}
```

The engine evaluates the condition symbolically against the substituted argument:

- If the call-site argument is a literal (`{{ tricky(True) }}`), the condition resolves and only the taken branch is walked.
- If the argument is a variable or non-literal expression, both branches are walked. Both `var('option_a')` and `var('option_b')` are recorded, with confidence set to partial and a note that the conditional could not be resolved.

Recording both branches when the condition is unresolved is the sound choice: it over-collects usages rather than missing one, and the partial-confidence marker tells the user the over-collection is deliberate.

## Depth limit and cycle detection

- A maximum recursion depth (the spec sets five levels) bounds pathological expansion.
- A call stack tracks the macros currently being expanded. A macro already on the stack is a cycle; the engine breaks it and marks the usage opaque with a recursive-macro reason rather than looping.

## The opacity rules

Several macro shapes cannot be followed statically. Each becomes an opaque diagnostic naming the reason, so the user knows exactly what needs manual declaration. The engine does not guess.

- **Runtime-dependent macros.** A macro that queries the warehouse during compilation (introspecting columns, checking relation existence) cannot be expanded statically. The engine detects these by reference to `run_query`, `statement`, or adapter introspection calls in the body, and treats the macro as opaque.
- **Adapter dispatch.** `{{ adapter.dispatch(...) }}` resolves at runtime by the configured adapter. The engine uses the adapter type from [discovery-inputs](./discovery-inputs.md) to pick the dispatch target and follows it normally; a missing target is opaque.
- **Higher-order macros.** Macros taking other macros as arguments are out of scope for v1; the engine marks them opaque with an unsupported-pattern reason and hints the user to declare the affected vars manually.
- **Custom Jinja extensions.** Most package extensions are pure text substitution and parse without issue under the configured environment. The few that perform side effects or non-standard parsing surface as parse failures and are treated as opaque, the same degrade path the [front end](./jinja-frontend.md) uses.

The unifying rule is the spec's: a usage we cannot resolve statically is recorded as opaque with a reason, never dropped and never guessed. An opaque usage degrades its var to a single resolved world downstream.

## Testing

- A name-resolution test (inherited from discovery-inputs) pinning the bare-name-then-package lookup order and the `package.name` direct path over `Manifest.macros`, plus a no-match yielding an opaque usage.
- Per-shape unit tests: a macro that wraps a `var()`, a macro that takes a `var()` as an argument, a macro with a literal-argument conditional (one branch walked), a macro with a non-literal conditional (both branches walked, partial confidence).
- A cycle test: two mutually recursive macros, asserting the engine terminates and marks the usage opaque rather than looping.
- A depth test: a chain past the limit, asserting it stops and marks opaque.
- An adapter-dispatch test resolving to a known target and following it.
- Opacity tests for a `run_query`-bearing macro and a higher-order macro, each asserting the opaque reason.

These pin the engine's contracts at the boundary (what usages come out, with what confidence and trail) rather than its internal recursion mechanics, so they survive a refactor of the walk.

## Open questions

- **Macro `depends_on.macros` versus walking.** The manifest records each macro's macro dependencies, now surfaced as `Macro.depends_on_macros`. Whether to use that edge set to bound or pre-filter the walk, or to walk purely from the body AST. Walking from the body is simpler and authoritative; the edge set may be a useful cross-check.
- **Argument binding fidelity.** dbt macros support defaults and `**kwargs`-style varargs in some patterns. How faithfully v1 binds these versus degrading to opaque on the exotic ones.

## References

- [The var-inference plan](./plan.md) and [the original spec](../var-inference-spec.md), "Macro handling".
- The parsed-body source: [the Jinja front end](./jinja-frontend.md). The registry: [discovery inputs](./discovery-inputs.md).
- The downstream effect of opacity (one resolved world): [inference and classification](./inference-and-classification.md).
</content>
