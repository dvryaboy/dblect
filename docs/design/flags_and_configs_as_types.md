# Flags and configuration in dblect

This document introduces dblect's flag system: how dbt vars and env_vars become typed configuration that dblect can reason about, how to declare and use flags, and what the framework does with them at PR time.

Audience: someone who has read the introductory dblect material, gets the pitch (static and runtime correctness checks for analytics pipelines, declared semantic types, contracts that survive refactors), and wants the next level of detail on the flag side without diving into implementation internals.

## The problem flags solve

A real dbt project has feature flags and configuration variables. Some control behavior (`use_late_arriving_handler`, `enable_v2_attribution`). Some carry semantic meaning (`include_tax_in_revenue`, `currency = "USD"`). Some are environment-specific (`environment = "prod"`). They all live in `dbt_project.yml` as vars, or in environment variables accessed via `env_var()`, and they all change what your models produce.

Two problems with flags as they exist today:

The data your pipeline produces depends on flag values, but your code only runs one configuration at a time. If you change a flag, you don't know what breaks until you actually run with the new value. A flag change can silently invalidate downstream assumptions for months until the affected configuration is finally exercised in production.

Flags interact with semantic types. If `include_tax_in_revenue` is True, your `revenue` column means something different than when it's False. Downstream models built against one meaning will break when it changes. The bug class is invisible to value-level diff tools when the flag hasn't actually been flipped yet, because the data looks identical until the configuration is exercised.

dblect's flag system addresses both: it lifts flags into the type system so they can be reasoned about statically, and it checks every configuration of your flags at PR time without running any of them.

## What a flag is in dblect's terms

A dblect flag is a Python class that represents a single piece of configuration. It carries five pieces of information:

- A **link** to the dbt var or env_var it represents
- A **type** (bool, enum, integer, string)
- A **domain** (the set of values the flag can take)
- A **default** (the value used when nothing else specifies)
- An **effect** (how the flag's value modifies refinement axes on semantic types)

Together these tell the framework what the flag is and what it does. The link, type, domain, and default come from your dbt project; dblect can typically infer them. The effect is the part you write, because it expresses what the flag *means* for your data.

The simplest flag looks like this:

```python
class IncludeTaxInRevenue(SemanticFlag):
    """When set, revenue values include sales tax."""
    dbt_var = "include_tax_in_revenue"
    type = bool
    default = False
    affects = RefinementEffect(
        target=Revenue.contains_tax,
        value_when_true=True,
        value_when_false=False,
    )
```

Five fields, a docstring, and you're done. The framework now knows that this flag exists, what values it can take, and how those values change the meaning of `Revenue` columns.

## Discovery: where flags come from

You usually don't write flag classes from scratch. You run:

```
dblect scaffold flags
```

The framework walks your dbt project, reads the manifest, and finds every `var()` and `env_var()` reference, including those reached through macros. For each one, it produces a draft `SemanticFlag` class. The draft has:

- The link to the dbt var pre-filled
- The type inferred from how the var is used in your SQL
- The domain inferred where possible (from equality comparisons, boolean usage, set-membership tests, or numeric branch points)
- The default pulled from `dbt_project.yml`
- The `affects` clause left as TODO for you to fill in

Your job is to review the drafts, write the docstrings and `affects` clauses, and either accept or correct the inferred type and domain.

A scaffolded boolean flag looks like:

```python
class IncludeTaxInRevenue(SemanticFlag):
    """TODO: describe what this flag controls."""
    dbt_var = "include_tax_in_revenue"
    type = bool
    domain = [True, False]
    default = False
    affects = ...  # TODO: declare the refinement effect

    # Inferred from:
    #   models/marts/fct_orders.sql:12 (truthy test)
    #   models/marts/fct_daily_summary.sql:8 (truthy test)
```

You see the source locations the inference relied on, so you can verify the framework picked up your var correctly. If it missed a usage, the source comments tell you where to look.

A scaffolded enum flag looks like:

```python
class Environment(SemanticFlag):
    """TODO: describe what this flag controls."""
    dbt_var = "environment"
    type = Enum["dev", "prod", "staging"]
    domain = ["dev", "prod", "staging"]  # tentative; review for completeness
    default = "prod"
    affects = ...  # TODO: declare the refinement effect
```

The "tentative" note on the domain is important. The framework infers the value set from what it saw in your SQL, but you may have additional values used in CI configs or environment variables that the static analysis couldn't see. Confirm the domain is complete before relying on it for world enumeration.

When inference fails entirely, you get a flag with placeholder TODOs and a diagnostic comment explaining why:

```python
class CustomPath(SemanticFlag):
    """TODO: declare type, domain, and affects."""
    dbt_var = "custom_path"
    type = ...  # TODO: inference failed
    domain = ...  # TODO
    default = "/tmp/data"
    affects = ...  # TODO

    # Inference failed: only usage is inside a macro that performs a runtime query.
    # Recommendation: declare type and domain manually.
```

In practice, inference works for the great majority of vars in typical dbt projects. The failures are concentrated in vars used inside complex macros or with runtime-dependent behavior, and the diagnostic output is specific enough that you know exactly what needs manual attention.

## The `affects` clause: what your flag does

The `affects` clause is the load-bearing part of a flag declaration, and the one piece dblect can't infer for you. It says how the flag's value maps to refinements on your semantic types.

The simplest case is a boolean flag that maps directly to a single refinement axis on a single type:

```python
affects = RefinementEffect(
    target=Revenue.contains_tax,
    value_when_true=True,
    value_when_false=False,
)
```

Read this as: "When the flag is True, any `Revenue` column produced by code that responds to this flag has `contains_tax=True`. When False, `contains_tax=False`."

For enum flags that affect a single axis, the mapping is a dictionary:

```python
class TaxJurisdiction(SemanticFlag):
    dbt_var = "tax_jurisdiction"
    type = Enum["US", "EU", "JP"]
    affects = RefinementEffect(
        target=Revenue.tax_regime,
        value_map={"US": "us_sales_tax", "EU": "vat", "JP": "consumption_tax"},
    )
```

For flags that affect multiple axes or multiple types, you combine effects:

```python
class StrictDeduplication(SemanticFlag):
    dbt_var = "strict_deduplication"
    type = bool
    affects = CompositeEffect(
        RefinementEffect(
            target=Customer.dedup_scope,
            value_when_true="strict",
            value_when_false="loose",
        ),
        RefinementEffect(
            target=Order.dedup_scope,
            value_when_true="strict",
            value_when_false="loose",
        ),
    )
```

The framework supports `CompositeEffect` for multi-axis cases and `ConditionalEffect` for cases where the mapping depends on other context. The vast majority of real flags use the simple single-axis form.

When a flag's effect is too complex to express in any of these forms, declare it with `affects = OpaqueEffect()` and treat it as type-erasing for the affected columns. This is the escape hatch. Use it sparingly, since it forfeits the static reasoning the framework would otherwise provide.

## World enumeration: what dblect does with your flags

Once your flags are declared, dblect does something straightforward but powerful: it enumerates every possible configuration of your flag values and propagates types through your SQL in each one. For each flag world, it produces a complete typecheck.

If you have three boolean flags, that's eight worlds. If you have a boolean flag and a three-value enum, that's six. The framework runs the analysis independently in each world and reports any world in which a declared contract fails.

The output looks like:

```
flag-world analysis for marts/discounts.sql

  world: include_tax_in_revenue=True, environment=prod
    PASS

  world: include_tax_in_revenue=False, environment=prod
    FAIL: revenue is declared Revenue(contains_tax=True) but inferred 
          Revenue(contains_tax=False) under this flag configuration

  world: include_tax_in_revenue=True, environment=dev
    PASS

  world: include_tax_in_revenue=False, environment=dev
    FAIL: same as above
```

This tells you: your `discounts` model works correctly when `include_tax_in_revenue` is True, but breaks when it's False. If the False configuration is used in any of your environments (or might be in the future), you have a latent bug that hasn't surfaced yet.

The framework does not run your SQL. It does not load any data. It does static type propagation under each flag assignment. The cost is roughly linear in the number of worlds times the cost of a single typecheck, which is fast. A project with five binary flags and a few hundred models typically completes in seconds.

For projects with many flags, dblect uses per-contract enumeration: each model contract only enumerates the flags that actually influence it, rather than the global product of all flag domains. This keeps analysis tractable even when the full flag space is large.

This is the headline capability the type system enables. Value-diff tools can tell you what changed in the data you have; flag-world analysis tells you what would change under configurations you haven't tried yet.

## Flags and contracts

Downstream models often have constraints on which flag configurations they support. You express this with `requires_flags` on the model contract:

```python
class Discounts(ModelContract):
    dbt_model = "marts.discounts"
    requires_flags = {"include_tax_in_revenue": True}
    
    discounted_revenue: Revenue = Field(contains_tax=True, contains_discount=True)
```

The contract says: this model is only expected to work when `include_tax_in_revenue` is True. Under other flag worlds, dblect skips the contract check for this model. This is useful for models that are intentionally configuration-specific.

For models with no `requires_flags` declaration, dblect checks the contract in every world. If a contract holds in some worlds but not others, the diagnostic tells you which worlds break.

The set of `requires_flags` declarations across your project effectively partitions your models by which configurations they support. dblect can produce a summary of which configurations the project supports end-to-end, which is useful for understanding the actual configuration space of a complex project. This is often hard to see from `dbt_project.yml` alone.

## Common patterns

A few patterns cover most real flag usage:

**Boolean toggle.** A single bool that flips a refinement axis. The `IncludeTaxInRevenue` example above is the canonical form. Most flag declarations in a typical project look like this.

**Mode enum.** An enum that selects between named modes, each mapping to a refinement value. Useful for validation strictness (`strict` vs `loose`), processing mode (`incremental` vs `full`), or pricing model (`gross` vs `net` vs `marketplace`).

**Environment-specific behavior.** An environment flag where the refinement effect may be the same across environments but the flag's presence in the world enumeration catches environment-dependent contract breaks. Useful when sandboxing affects what data is available or when different environments use different upstream sources.

**Numeric threshold.** A numeric flag where dblect enumerates worlds around branch points observed in your SQL. If your code has `var('threshold') > 100`, dblect enumerates two worlds (threshold ≤ 100, threshold > 100) and propagates types through each. Useful for sampling rates, batch sizes, and confidence cutoffs.

**Currency flag.** A flag whose effect targets a `currency` axis on monetary types. Useful for multi-currency projects, often combined with a `tax_jurisdiction` flag for full multi-region support.

If your flag doesn't fit any of these patterns, you'll likely need `CompositeEffect` (multi-axis) or `ConditionalEffect` (context-dependent). If those don't fit either, the flag is doing something that the type system can't cleanly express, and you should either restructure it or fall back to `OpaqueEffect`.

## When inference is incomplete

Even with macro following and domain inference, a portion of vars in any real project will inference-fail or partially infer. The common causes:

*Macros that query the warehouse during compilation.* These can't be statically expanded. dblect treats them as opaque and you declare the affected vars manually.

*Vars used only as macro arguments without comparison.* The framework sees the call but not what's done with the value. Type may be inferable, but domain isn't.

*Higher-order macros and exotic Jinja patterns.* Rare in practice, but the framework declines to follow them.

*Vars whose domain is genuinely open.* A free-form string used as a path or identifier. The framework gives you type but not domain; flag-world analysis is limited or skipped.

For each of these, the scaffold output tells you exactly what couldn't be inferred and why. Your options:

1. **Declare manually.** Fill in the type, domain, and affects yourself. Works for any case.
2. **Refactor the calling code.** Sometimes the inference fails because of unusual Jinja patterns. Simplifying the calling code can let the inference succeed on the next run.
3. **Accept partial coverage.** A flag with inferred type but no inferred domain still gets some flag-world analysis (the framework treats the default value as the only enumerated world). You lose the "explore configuration space" benefit for that flag but keep everything else.

Across real projects, the fully-inferred case is the majority. Inference failures concentrate in custom macro infrastructure, often of the kind that benefits from refactoring anyway.

## Re-running the scaffold

You can run `dblect scaffold flags` repeatedly as your project evolves. New vars get new draft classes; existing classes are preserved if they have non-default `affects` clauses (the framework treats those as user-owned).

The framework updates the inference-derived fields (type, domain, source-location comments) and leaves your manual edits alone. If a class's inferred type changes between runs because of SQL changes, you get a diagnostic noting the difference; the existing class is preserved unchanged and you decide whether to accept the new inference.

This means re-running scaffold is safe and is the recommended way to keep your flag declarations in sync with your dbt project. Run it whenever you add or change a var, and review the diff.

## How flags compose with the broader type system

Flags are a level up from columns. A column annotation says "this column has refinement X." A flag declaration says "when this flag has value V, columns produced by responsive code have refinement X." Flags parameterize entire models the way columns parameterize entire values.

This composition is what makes dblect's static analysis a genuine type system rather than a one-shot checker. Type propagation through SQL is purely structural in each world; flag worlds add a parameterization over the propagation. The same mechanics handle both, which is why adding flags doesn't multiply the framework's complexity.

For everyday use, the implication is straightforward: declare the columns you care about, declare the flags that affect them, and let the framework handle the combinatorial reasoning. You write things that look like Pandera schemas; the framework does the configuration-space exploration.

### Switch types as an optional convenience

An alternative authoring surface (a `Revenue.switch(on=flag, cases={True: ..., False: ...})` shorthand on the type itself) was considered as an early-iteration design. The canonical surface is the `SemanticFlag` class with `affects = RefinementEffect(...)` shown throughout this document, because it scales cleanly to flags that target multiple axes or multiple types (`CompositeEffect`, `ConditionalEffect`) and keeps the registry of flag effects in one place. A `switch()` shorthand may still ship as a thin convenience that produces the same registry entry as a single-axis `RefinementEffect`, if the engineering cost is small. Whether to bother is unsettled; nothing in the rest of the design depends on it.

## What's coming, what's deferred

The v1 flag system covers:

- `dbt var()` and `env_var()` discovery from the manifest
- Type inference from Jinja usage patterns
- Domain inference from comparison evidence, set membership, and numeric branch points
- Macro following with depth-limited recursion
- World enumeration and contract checking under each world
- The five `RefinementEffect` shapes: direct, value-map, composite, conditional, opaque

What's deferred to later versions:

*Per-entity flags from seed-based config tables.* The `customer_config.tax_inclusive` pattern, where flags vary per row rather than per dbt run. Structurally different from global flags and gets its own design pass.

*External flag platforms.* LaunchDarkly, Statsig, Unleash, OpenFeature. These have warehouse exports we can read, and integration code is shipped as adapters in later releases.

*Cross-package flag inference.* Flags declared in one dbt package and referenced from another that doesn't import it. Requires manifest-spanning analysis.

*Application-side flags that don't surface in the warehouse.* Flags evaluated in producer code that change the shape of data written but never appear as columns. You can declare these manually if needed, but the framework can't auto-discover them.

In every deferred case, the underlying flag machinery is the same. Future versions add discovery adapters and per-entity world enumeration on top of the v1 flag substrate. Code you write against the v1 surface stays valid.

## Where to go next

For deeper material:

- *The type theory tutorial.* The mathematical underpinnings: refinement types, lattices, propagation, soundness. Useful if you want to understand what the framework is actually computing or how to design custom refinement axes.
- *The var inference spec.* The implementation of var discovery and inference, including the Jinja AST walker and macro expansion. Useful if you're contributing to the framework or debugging an inference issue.
- *The model contracts guide.* How to declare and use the `ModelContract` and `Field` machinery that flags compose with. Useful as a counterpart to this guide.

For practical work:

- Start with `dblect scaffold flags` on your project.
- Fill in `affects` clauses for the flags that matter most.
- Run `dblect check` and read the flag-world output.
- Iterate.

The investment is incremental and the framework gives useful output at every step. You don't need to declare every flag before you start getting value; you need to declare the ones that affect the critical chain.
