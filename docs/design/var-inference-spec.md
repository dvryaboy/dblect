# Var inference and flag scaffolding: technical specification

Status: draft for implementation
Audience: engineers implementing dblect's flag discovery and inference layer
Scope: v1 of var handling, covering `var()` and `env_var()` only

## Overview

dblect needs a way to discover the feature flags and configuration variables a dbt project uses, infer enough about each one (type, domain, default) to enumerate worlds for type propagation, and produce scaffolding that the user reviews and completes.

The user-facing surface is a single command, `dblect scaffold flags`, that walks a dbt project, identifies every `var()` and `env_var()` reference, performs static analysis to infer the type and value domain of each, and writes a Python file of draft `DomainFlag` classes alongside a diagnostic report. The user reviews the draft, fills in the semantic effect of each flag (the `affects` clause that the static analysis cannot determine), and accepts the scaffold into the project.

This document specifies the algorithm, the output format, and the implementation phasing.

## Goals

1. Discover all `var()` and `env_var()` references across the dbt project, including those reached through macros.
2. Infer the type of each variable (boolean, enum, integer, string, etc.) where the source SQL gives sufficient static evidence.
3. Infer the value domain (set of possible values) where evidence supports it.
4. Produce draft `DomainFlag` classes the user can review and complete.
5. Report unfollowed usages and incomplete inferences clearly so the user knows where manual work is needed.

## Non-goals

1. Inferring the semantic effect of a flag (the `affects` clause). This requires domain knowledge that static analysis cannot provide.
2. Handling external feature flag platforms (LaunchDarkly, Statsig, OpenFeature) or per-entity configuration tables. Deferred to later versions.
3. Runtime evaluation of macros that depend on warehouse state.
4. Cross-project inference (a flag declared in one dbt package and referenced in another that doesn't import it).

## Inputs

1. **dbt manifest** produced by `dbt parse`. Contains:
   - The list of all models with their source SQL and compiled SQL
   - The list of all macros from the project, from installed packages, and from dbt built-ins, with source bodies
   - The list of all `var()` and `env_var()` references per model (names only, no contexts)
   - Project configuration including declared vars and their defaults

2. **dbt project configuration files**:
   - `dbt_project.yml` for declared vars and their defaults
   - `profiles.yml` for target-specific var overrides

3. **The adapter type** (Snowflake, BigQuery, DuckDB, etc.) from the active profile, used to resolve `adapter.dispatch` calls.

## Outputs

1. **Generated Python file** (default `dblect/flags/discovered.py`) containing one `DomainFlag` subclass per discovered variable. Each class includes:
   - The dbt var or env_var name
   - The inferred type
   - The inferred domain (where applicable)
   - The default value (where declared)
   - Source-location comments showing where the variable was used
   - A `TODO` marker on the `affects` clause and the docstring

2. **Diagnostic report** (default `dblect/flags/discovery_report.md`) listing:
   - All variables found with their inferred properties
   - Usages that couldn't be statically resolved, with locations and reasons
   - Suggestions for manual declaration in cases of inference gaps

## Algorithm

The algorithm runs in four stages: discovery, per-model analysis, aggregation, and output generation.

### Discovery

1. Invoke `dbt parse` (or read an existing manifest) to produce `manifest.json`.
2. Load all macros from the manifest into a name-indexed registry, including project macros, package macros, and dbt built-ins.
3. Load `dbt_project.yml` to extract declared vars and their defaults.
4. Load `profiles.yml` (if present and accessible) to extract target-specific var overrides.
5. Determine the active adapter from the configured target.

### Per-model analysis

For each model in the manifest:

1. Read the source SQL from the manifest.
2. Parse the source with Jinja2's parser to produce an AST.
3. Walk the AST collecting `VarUsage` records (defined below) for every `var()` and `env_var()` call, capturing the syntactic context of the call.
4. When a macro call is encountered, perform macro expansion (see "Macro handling" below) and continue the walk into the expanded body.
5. Track the call stack to detect cycles and enforce the depth limit.

A `VarUsage` record captures:

```python
class VarUsage:
    var_name: str                   # the name passed to var() or env_var()
    var_kind: Literal["var", "env_var"]
    context: UsageContext           # see below
    location: SourceLocation        # file, line, column
    macro_trail: list[str]          # macros traversed to reach this usage
    confidence: Confidence          # full, partial, opaque
```

`UsageContext` distinguishes the syntactic position of the call:

- `TruthyTest`: appears as the test in `{% if var('x') %}` or equivalent
- `Equality(operand)`: appears as one side of `var('x') == operand`
- `Inequality(operand, op)`: appears in `<`, `>`, `<=`, `>=` against an operand
- `InSet(elements)`: appears as `var('x') in [a, b, c]`
- `Arithmetic(op, other)`: appears in `var('x') + 1`, `var('x') * 2`, etc.
- `SqlLiteral(position)`: appears in compiled SQL as a literal value, with hints about position (string-quoted, numeric, identifier)
- `MacroArg(macro, position)`: passed as an argument to a macro we could not follow
- `Unknown`: any other position

### Aggregation

After all models are analyzed:

1. Group `VarUsage` records by variable name.
2. For each variable, run type inference (see "Type inference rules") across all its usages.
3. For each variable, run domain inference (see "Domain inference rules") across all its usages.
4. Cross-reference with `dbt_project.yml` defaults and `profiles.yml` target-specific values.
5. Produce a `DiscoveredVar` record per variable:

```python
class DiscoveredVar:
    name: str
    kind: Literal["var", "env_var"]
    inferred_type: Type | None      # None means we couldn't determine type
    inferred_domain: Domain | None  # None means open or unknown domain
    default: Value | None           # from dbt_project.yml
    target_values: dict[str, Value] # from profiles.yml
    usages: list[VarUsage]
    inference_quality: Quality      # full, partial, type_only, none
    unfollowed_usages: list[UnfollowedUsage]
```

### Output generation

1. Render each `DiscoveredVar` as a Python `DomainFlag` class (template below).
2. Sort classes alphabetically and write to the output file.
3. Generate the diagnostic report.

## Type inference rules

Type inference proceeds by examining each `VarUsage`'s context and producing a type assertion. Assertions from multiple usages combine via the type lattice: compatible assertions narrow the type, incompatible assertions indicate ambiguity (reported as a conflict in the diagnostic output).

| Usage context | Type assertion |
|---|---|
| `TruthyTest` | `bool` (loose: any value the var takes is interpreted as truthy or falsy) |
| `Equality(literal)` where literal is a bool | `bool` |
| `Equality(literal)` where literal is a string | `str` |
| `Equality(literal)` where literal is a number | `int` or `float` matching literal type |
| `Inequality(literal, op)` where literal is a number | `int` or `float` |
| `InSet(elements)` with homogeneous elements | type of elements |
| `Arithmetic(op, number)` | numeric (`int` or `float` based on operands) |
| `SqlLiteral` in numeric position (e.g., `LIMIT {{ var('x') }}`) | numeric |
| `SqlLiteral` in quoted-string position | `str` |
| `SqlLiteral` in identifier position (e.g., `FROM {{ var('schema') }}.users`) | `str` (with note: SQL identifier) |
| `MacroArg` | type unknown unless macro can be followed |
| `Unknown` | type unknown |

When multiple contexts produce different type assertions for the same variable, this is a conflict. Conflicts are reported in the diagnostic output. The most permissive type wins for scaffolding purposes, with a comment noting the conflict.

The default value from `dbt_project.yml` provides an additional type signal. A boolean default with no usage evidence to the contrary infers `bool`. A string default with no usage evidence infers `str`. Numeric defaults infer `int` or `float`.

## Domain inference rules

Domain inference identifies the set of values a variable can take. The default stance is open-world (the domain is unbounded) unless evidence supports a finite domain.

A variable has a **finite inferred domain** if:

- All observed usages are `Equality(literal)` or `InSet(elements)` contexts, and the union of those literals is the domain. The variable's `default` and target-specific values must also be members of the inferred domain, or the inference is marked as inconsistent.
- The variable is boolean (type `bool` always has a finite two-element domain).

A variable has a **partial inferred domain** if:

- Some usages support a finite domain and some don't (e.g., `var('x') == 'literal'` and `var('x') in some_other_context`). The inferred domain is the union of observed literals, with a note that other values may exist.

A variable has **branch points** if numeric comparisons are observed. The branch points partition the number line into intervals; world enumeration covers each interval rather than each individual value. For example, `var('threshold') > 100` produces two worlds (`threshold <= 100` and `threshold > 100`).

Domain inferences from observation are always reported as "tentative, review for completeness" in the scaffolded output. The user has the final say on whether the closed-world reading is correct.

Cross-references:
- Default values from `dbt_project.yml` are added to the inferred domain.
- Target-specific values from `profiles.yml` are added to the inferred domain.
- These count as observed values without changing the open vs. finite classification.

## Macro handling

Macro expansion is required to follow `var()` calls that are reached through macro indirection.

### Lookup

The dbt manifest provides every macro's source body keyed by macro name. The lookup is a dictionary access; no scanning is required.

### Substitution

When a macro call `{{ get_flag('include_tax') }}` is encountered during AST walking:

1. Look up the macro definition in the registry.
2. Parse the macro body with Jinja2 to produce an AST.
3. Substitute the call-site arguments for the macro's parameters in the AST (lexical substitution).
4. Walk into the substituted AST and continue collecting `VarUsage` records.
5. Push the macro name onto the call stack; pop after the walk completes.

The walk continues recursively when the macro body itself contains macro calls.

### Depth limit and cycle detection

- A maximum recursion depth of 5 levels prevents pathological cases.
- A call stack tracks the macros currently being expanded. If a macro is already in the stack, the cycle is broken and the usage is marked as opaque with a "recursive macro" reason.

### Internal control flow

When a macro body contains Jinja control flow whose condition depends on a macro argument:

```jinja
{% macro tricky(condition) %}
  {% if condition %}
    {{ var('option_a') }}
  {% else %}
    {{ var('option_b') }}
  {% endif %}
{% endmacro %}
```

The expansion logic evaluates the condition symbolically:

- If the call-site argument for `condition` is a literal (`{{ tricky(True) }}`), the condition evaluates and the appropriate branch is walked.
- If the call-site argument is a variable or expression, both branches are walked. Both `var('option_a')` and `var('option_b')` are recorded as potential usages, with the `confidence` field set to `partial` and a note that the conditional could not be resolved.

### Higher-order macros

Macros that take other macros as arguments are out of scope for v1 inference. When encountered, the call is marked as opaque with an "unsupported pattern" reason. The user receives a hint to declare the affected vars manually.

### Runtime-dependent macros

Some macros query the warehouse during compilation (e.g., to introspect column lists). The manifest does not mark these explicitly, so the expansion logic detects them by failed parsing or by reference to `run_query`, `statement`, or adapter introspection calls. When detected, the macro is treated as opaque.

### Adapter dispatch

`{{ adapter.dispatch(...) }}` resolves at runtime based on the configured warehouse adapter. The expansion logic uses the adapter type from the active profile to pick the appropriate dispatch target, then follows that macro normally. If the dispatch target is not found, the call is marked as opaque.

### Custom Jinja extensions

Packages like `dbt-utils` and `dbt-expectations` register custom Jinja extensions. Most are pure text substitution and parse without issue. The few that perform side effects or non-standard parsing are detected by AST parse failure and treated as opaque.

## Output format

### Generated Python file

The scaffolded file is written to `dblect/flags/discovered.py` by default (overridable). Each variable produces a class:

```python
class IncludeTaxInRevenue(DomainFlag):
    """
    Auto-discovered from dbt var. TODO: describe what this flag controls.
    """
    dbt_var = "include_tax_in_revenue"
    type = bool
    domain = [True, False]
    default = False
    affects = ...  # TODO: declare the refinement effect

    # Inferred from:
    #   models/marts/fct_orders.sql:12 (truthy test)
    #   models/marts/fct_daily_summary.sql:8 (truthy test)
```

For enum-typed variables with inferred finite domains:

```python
class Environment(DomainFlag):
    """
    Auto-discovered from dbt var. TODO: describe what this flag controls.
    """
    dbt_var = "environment"
    type = Enum["dev", "prod", "staging"]
    domain = ["dev", "prod", "staging"]  # tentative; review for completeness
    default = "prod"
    affects = ...  # TODO: declare the refinement effect

    # Inferred from:
    #   models/staging/_sources.yml: target.name == 'dev'
    #   profiles.yml: targets dev and prod
    #   macros/get_env_schema.sql:4 (var('environment') == 'staging')
```

For variables whose type was inferred but whose domain remains open:

```python
class Limit(DomainFlag):
    """
    Auto-discovered from dbt var. TODO: describe what this flag controls.
    """
    dbt_var = "limit"
    type = int
    domain = None  # open; observed branch points at 100, 1000
    default = 1000
    affects = ...  # TODO: declare the refinement effect

    # Inferred from:
    #   models/marts/fct_top_customers.sql:9 (LIMIT clause)
    #   models/marts/fct_top_orders.sql:11 (var('limit') > 100)
```

For variables that could not be inferred at all:

```python
class CustomPath(DomainFlag):
    """
    Auto-discovered from dbt var. TODO: declare type, domain, and affects.
    """
    dbt_var = "custom_path"
    type = ...  # TODO: inference failed (see report)
    domain = ...  # TODO
    default = "/tmp/data"
    affects = ...  # TODO: declare the refinement effect

    # Inferred from:
    #   macros/get_custom_path.sql (opaque: runtime warehouse query)
```

### Diagnostic report

The diagnostic report is a markdown file summarizing the discovery:

```
# Flag discovery report

Generated on 2026-05-19 at 14:32 UTC by dblect 0.1.0
Project: my_dbt_project
Manifest: target/manifest.json

## Summary

Total vars discovered: 12
Total env_vars discovered: 3
Fully inferred: 9
Partially inferred: 4
Inference failed: 2

## Variables

### include_tax_in_revenue (fully inferred)
- Type: bool
- Domain: [True, False]
- Default: False
- Usages: 2 (all in conditional control flow)
- Status: ready for affects clause

### environment (fully inferred)
- Type: Enum["dev", "prod", "staging"]
- Domain: tentative (review for completeness)
- Default: prod
- Usages: 8 across 5 models
- Status: ready for affects clause; confirm domain

### custom_path (inference failed)
- Type: unknown
- Reason: only usage is inside a macro that performs a runtime query
- Recommendation: declare type and domain manually

## Unfollowed usages

### macros/custom_extension.sql:14
- Macro: complex_dispatch
- Reason: higher-order macro pattern not supported in v1
- Affected vars: feature_a, feature_b
- Recommendation: declare these vars manually or refactor the macro
```

## Implementation stages

The implementation breaks into three weekly stages, each producing a working deliverable.

### Basic discovery and direct usage inference (week 1)

- Manifest reading and project file parsing
- Jinja2 AST walker for direct `var()` and `env_var()` references
- Type inference rules for direct usage contexts
- Domain inference rules for direct usage contexts
- Generated Python file output
- Basic diagnostic report

At the end of this stage, the tool handles projects whose vars are all referenced directly (no macro indirection). This covers the majority of small dbt projects.

### Macro following (week 2)

- Macro lookup from the manifest
- Recursive expansion with depth limit and cycle detection
- Lexical parameter substitution
- Symbolic evaluation of literal-argument conditionals
- Adapter dispatch resolution
- Per-usage confidence tracking through macro trails

At the end of this stage, the tool handles projects with non-trivial macro use. The jaffle-shop generator and dbt-utils-heavy projects become tractable.

### Polish and edge cases (week 3)

- Custom Jinja extension handling (parse what we can, opaque the rest)
- Runtime-dependent macro detection
- Higher-order macro detection
- Cross-reference with profiles.yml
- Diagnostic report quality improvements
- Integration testing against several real dbt projects

At the end of this stage, the tool is ready for the v1 release. The known limitations are documented and surfaced clearly in the diagnostic report.

## Testing strategy

### Unit tests

Synthetic Jinja templates with known var usages, covering each inference rule. One template per rule, with expected `VarUsage` and `DiscoveredVar` outputs.

### Integration tests

Real dbt projects committed to the repository as fixtures:

- jaffle-shop (vanilla): baseline coverage
- jaffle-shop-generator output: realistic complexity
- A project using dbt-utils heavily: macro following
- A project using custom Jinja extensions: opaque handling
- A multi-target project with profile-specific vars: cross-reference logic

For each fixture, snapshot the expected scaffold output and diagnostic report. Tests compare against the snapshots.

### Regression suite

As we encounter dbt projects in the wild that trigger new patterns, add minimized versions to the fixture set with the new expected output. The fixture set grows over time and protects against regressions.

## Open questions

1. **Vars used only in seeds, sources, or documentation:** dbt vars can appear in non-model contexts. The initial discovery stage covers models only. Should we extend to seeds and sources in the macro-following stage or defer to v2?

2. **Closed-world vs open-world default for inferred domains:** Currently the spec marks inferred enum domains as "tentative." Is this strong enough, or should we surface a more explicit confirmation step before treating the inferred domain as authoritative for world enumeration?

3. **Vars declared in dbt_project.yml but never used:** Should the scaffold emit a `DomainFlag` class anyway (since the var is declared), or skip it with a note? Argument for emitting: the var may be used in the future. Argument for skipping: noise reduction.

4. **Per-package var inference:** Some vars are declared by installed packages (e.g., `dbt-utils:dispatch_list`). Should these be included in the scaffold output, or filtered out as "not the user's vars"? Probably filter, but worth confirming.

5. **Re-running scaffold:** What happens when `dblect scaffold flags` is run a second time, after the user has filled in `affects` clauses on some classes? The tool needs to merge cleanly without clobbering manual edits. Proposal: detect classes with non-default `affects` and preserve them; update only inference-derived fields. Worth confirming the merge logic before implementation.

6. **Versioning the scaffold output:** Should the generated file include a version marker so future tool versions can detect and migrate older formats? Probably yes; v1 starts at format version 1.

## Appendix: Reference types

`DomainFlag` is defined in `dblect/types/flag.py` (specification elsewhere). For this spec, the relevant interface is:

```python
class DomainFlag:
    dbt_var: str | None         # the dbt var name (None for env_vars)
    env_var: str | None         # the env_var name (None for dbt vars)
    type: type | EnumType       # the inferred or declared type
    domain: list | None         # finite domain, or None for open
    default: Any | None         # the default value
    affects: RefinementEffect   # the semantic effect (user-declared)
```

`RefinementEffect` is defined in `dblect/types/effect.py`. It expresses how the flag's value maps to refinement axis values on declared domain types.

`Domain` for finite domains is a list of literal values; for branch-point partitioned numeric domains, it's a list of intervals.

## Appendix: Glossary

- **var**: a dbt variable declared via `{{ var('name') }}` or in `dbt_project.yml`
- **env_var**: an environment variable accessed via `{{ env_var('NAME') }}`
- **flag**: a typed wrapper around a var or env_var with declared semantic effect; a `DomainFlag` subclass
- **scaffold**: the generated draft Python file produced by inference
- **world**: a specific assignment of values to flags, used during type propagation
- **domain**: the set of possible values a variable can take
- **refinement effect**: how a flag's value modifies the refinements on domain types
- **opaque usage**: a var usage we could not statically resolve (typically inside an unfollowable macro)
