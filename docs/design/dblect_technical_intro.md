# dblect: contracts and domain types in Python

*Status: working design notes. Captures current direction. Internal AST shape and some API details remain unsettled; overall shape is settled. The Python approach below evolved out of an earlier exploration that placed expressions inside YAML strings. Filename kept for continuity.*

## What this is

dblect's contracts and domain types are written in Python. Declarations live in plain Python files that look and feel like Pydantic models and Hypothesis strategies, because that's what they fundamentally are: a typed schema layer over the dbt DAG that doubles as a generator specification for property-based testing.

This builds on a Python idiom that's well-developed in adjacent territory. SQLAlchemy, Pandera, Polars, Ibis, dlt, and dbt-Pydantic-contracts all use host-language type annotations with operator-overloaded column proxies. Each has worked through the ergonomic tradeoffs over years of production use, and the pattern has earned its popularity by giving users autocomplete, type-checking, refactor-rename, and jump-to-definition for free.

The contract surface is *still data*. Contracts produce an internal AST the framework consumes for static analysis, change-impact reporting, Hegel compilation, and the MCP tools. The data form just happens to be derived from Python source rather than parsed from strings.

## Heroes

Three libraries shape the API design most directly. **Pydantic** sets the pattern for the declaration surface: classes with type-annotated fields and `Field(...)` metadata, where the class serves as both Python code and structured data. **Hypothesis** sets the pattern for the verification surface: declarative strategies, shrinking to minimal counterexamples, generators that come from type information rather than hand-written code. Hegel descends from Hypothesis directly, so the inheritance is structural. **SQLAlchemy** sets the pattern for the expression builder: column objects whose Python operators produce symbolic ASTs rather than evaluating, with the AST compiling to different backends at execution time.

One additional library serves as core machinery rather than as a pattern source. **sqlglot** is dblect's SQL substrate: it parses the compiled dbt SQL the static analyzer walks, provides the column-level lineage that drives type propagation, supplies the base type system that `dblect.types` wraps, and handles cross-dialect SQL rendering for the runtime verification path. Without sqlglot the framework wouldn't function; with it, large parts of what dblect does for analytics SQL come essentially for free.

A longer accounting of influences, including Pandera, Ibis, Polars, mypy, MetricFlow, Prisma, dlt, pytest, and sqlglot, appears at the end of the doc under [Influences and what we borrow](#influences-and-what-we-borrow). Reading that section is optional for understanding the design; it documents lineage for readers trying to place dblect mentally against tools they already know.

## Core surface

Four kinds of declarations, each a Python class or decorated function. The running examples are jaffle-shop-shaped: orders, customers, products, line items, returns.

### Domain types

A domain type is a class derived from `dblect.DomainType` declaring its base SQL type and its refinement axes. DomainTypes are scalar: each type wraps one SQL column. The base type is declared as a Pydantic-style annotated field; refinement axes are annotated fields whose values are Python primitives or enums.

```python
import dblect
from dblect.types import Decimal

class Revenue(dblect.DomainType):
    amount: Decimal(18, 2)      # base SQL type
    contains_tax: bool          # is sales tax included?
    contains_discount: bool     # is this post-discount or pre-discount?
    currency: str
```

Refinement uses explicit method calls to derive specific variants, a settled design call:

```python
RevenueGross = Revenue.refine(contains_tax=False, contains_discount=False)
# list price × quantity, before any discounts or tax (what your catalog says)

RevenueNet = Revenue.refine(contains_tax=False, contains_discount=True)
# after discounts, before tax (common accounting "net revenue")

RevenueCollected = Revenue.refine(contains_tax=True, contains_discount=True)
# what actually hits the bank (gross of tax, net of discount)
```

These three are different numbers measuring related but distinct things. The bugs dblect targets are exactly the ones where one of these gets used where another was assumed.

Multi-column concepts (money with an explicit per-row currency column, address-with-parts, range-with-start-end) are modeled as separate columns linked by contracts rather than as record-shaped types. The rule of thumb: per-row varying values stay as their own columns and get cross-column contracts; values globally fixed for a model (or pinned by a flag) become refinement axes so the static type layer catches mismatches at PR time.

Flag-conditional refinement is declared via the `DomainFlag` system rather than on the type itself. A separate `DomainFlag` class names the dbt var, declares its type and domain, and binds via an `affects = RefinementEffect(...)` clause that names which refinement axis on which type the flag controls. See [Flags and configuration in dblect](flags_and_configs_as_types.md) for the full pattern.

### Model contracts

A `ModelContract` class binds domain types to columns of a specific dbt model and attaches the contracts the model must satisfy.

```python
from dblect import models, ModelContract, Field, contract
from .types import RevenueNet, TaxAmount, OrderId, CustomerId, OrderDate

class FctOrders(ModelContract):
    """One row per order with order-level totals."""
    
    dbt_model = "marts.fct_orders"
    
    # column declarations
    order_id: OrderId
    customer_id: dblect.ForeignKey("dim_customers.customer_id")
    order_date: OrderDate
    order_total: RevenueNet = Field(ge=0)
    tax_paid: TaxAmount = Field(ge=0)
    
    # contract methods
    @contract
    def order_total_matches_line_items(self):
        """Order header total reconciles to sum of line item subtotals."""
        return (
            self.order_total.sum().group_by(self.order_id)
            == models.stg_order_items.subtotal.sum()
                .group_by(models.stg_order_items.order_id)
        ).within(0.01)
    
    @contract
    def one_row_per_order(self):
        return self.grain(per=self.order_id)
    
    @contract.replay_class("deterministic")
    def deterministic_under_inputs(self): ...
```

A few features earning their place:

- `dbt_model = "marts.fct_orders"` binds the class to a specific dbt manifest entity. Resolution happens at framework-load time rather than at class-definition time, so circular references and missing models surface as findings rather than import errors.
- Column annotations are domain types from your project's type registry. `Field(...)` adds refinements and column-level constraints.
- `dblect.ForeignKey("dim_customers.customer_id")` is a parameterized type referencing another model's column. The FK-aware fixture builder uses this to construct coordinated multi-table inputs.
- The `@contract` marker wraps a method. The method body builds an AST via operator-overloaded column proxies, with column references like `self.order_total` and `models.stg_order_items.subtotal` returning symbolic placeholders rather than actual values. The framework introspects the AST to do static analysis, change-impact, and Hegel compilation.
- `self.where(...)` scopes a contract to rows matching a predicate. Composes with any contract.

> Note: `replay_class` above, and the late-arrival contracts in the same family, are the least developed part of the contract surface. They reach into replay determinism and temporal consistency, work that is not underway yet, so their spelling is provisional and will be reworked once that work begins rather than treated as settled.

### Standalone contracts

Some contracts don't belong on a single model. Cross-model contracts without a clear "owning" model live as standalone declarations:

```python
from dblect import contract, models

@contract(when=models.fct_revenue_by_store.exists())
def store_breakdown_reconciles_to_total():
    """If we have a per-store revenue breakdown, its total matches the company-wide total."""
    return (
        models.fct_revenue_by_store.revenue.sum()
        .within(0.001).relative_to(models.fct_total_revenue.revenue.sum())
    )
```

This is the reconciliation pattern: a breakdown table whose total should match a non-broken-down version of the same underlying fact. Universal across analytics shops. Sources of bugs include filter drift (the breakdown table filters out something the total doesn't), double counting (a join in the breakdown fanout-multiplies rows), and stale incremental refresh (one table updates, the other doesn't).

- `@contract(when=...)` is a precondition. Here it's a structural predicate (does the model exist in the manifest?), and it can also be a runtime predicate over generated data.
- The contract is a function rather than a method, because it isn't naturally attached to any one model.
- `.within(0.001).relative_to(...)` is the relative-tolerance idiom, expressed as method chaining on the column proxy.

### Contract groups and shared scope

When multiple contracts share preconditions or scope, a `ContractGroup` reduces repetition:

```python
class ReturnReconciliation(dblect.ContractGroup):
    """Contracts that apply when the returns table is populated."""
    
    when = models.fct_returns.row_count() > 0
    
    @contract
    def refunds_never_exceed_originals(self):
        """You can't refund more than was originally charged."""
        return (
            models.fct_returns.refund_amount.sum()
            <= models.fct_orders.order_total.sum()
        )
    
    @contract
    def returned_quantity_at_most_sold(self):
        """For any product, returns can't exceed sales (per all-time)."""
        return (
            models.fct_returns.returned_quantity.sum()
                .group_by(models.fct_returns.product_id)
            <= models.fct_order_items.quantity.sum()
                .group_by(models.fct_order_items.product_id)
        )
```

This is the Pydantic settings-class pattern applied to contracts: a class with shared metadata and several methods that inherit it.

## Base types

`dblect.types.Decimal`, `dblect.types.Integer`, `dblect.types.Date`, and friends are thin re-exports of sqlglot's `DataType` with convenience constructors. There's no dblect-specific wrapper class hierarchy parallel to sqlglot's; dblect uses sqlglot's types directly. The framework-specific behaviors (Hypothesis/Hegel strategies for value generation, coercion checks, refinement-axis composition) live in *free-function dispatch tables* keyed by sqlglot type rather than as methods on a wrapper class.

```python
# dblect.generators (sketch)
@strategy_for.register(sqlglot.exp.DataType.Type.DECIMAL)
def _(t, **kwargs):
    p, s = t.expressions  # precision, scale
    return strategies.decimals(places=s, **kwargs)

# dblect.coercion (sketch)
@coerces_to.register(sqlglot.exp.DataType.Type.DECIMAL,
                     sqlglot.exp.DataType.Type.DECIMAL)
def _(a, b):
    return a.precision <= b.precision and a.scale <= b.scale
```

The implementation footprint is small: ~30 lines of namespace plumbing for the user-facing imports, plus dispatch tables for the dispatching behaviors. The actual SQL semantics, dialect rendering, parsing, and parameter handling all live in sqlglot underneath.

### Why not SQLAlchemy

SQLAlchemy has the most comprehensive Python SQL type system, and it would work as the underlying representation. The argument against is dependency weight. SQLAlchemy is built to participate in the SQLAlchemy ecosystem (Column metadata, Table objects, session machinery), and pulling those types out to use standalone fights the original design. dblect doesn't otherwise need SQLAlchemy, so importing it for just the type vocabulary is a larger commitment than the problem warrants. sqlglot, which is already a hard dependency for parsing and analysis, supplies what we need with a much smaller imported surface.

### Connecting to the dbt manifest

dbt's manifest carries column `data_type` strings like `"DECIMAL(10,2)"` and `"VARCHAR(255)"`. sqlglot parses these into typed objects via `sqlglot.parse_one(data_type_string, into=DataType)`. No wrapping or remapping needed; dblect works with the parsed sqlglot types directly. The framework cross-checks user-declared domain types against the actual SQL types in the warehouse via the coercion dispatch: a column declared as `RevenueNet` (whose base annotation is `Decimal(18, 2)`) should be backed by a `DECIMAL` or `NUMERIC` column in the database, and a mismatch surfaces as a finding.

### Decimal is the tricky case

Most base-type behaviors are simple. Decimal interacts with the framework in more ways than the others because of conservation contracts: sums of decimals, comparisons across different precisions, tolerance specifications expressed in decimals or percentages, the question of what `sum(Decimal(10,2)) + sum(Decimal(8,4))` should type as. The coercion dispatch rules, generator strategy bounds, and rendering all need to handle precision-and-scale correctly before adding the long tail of less-common types. Worth getting Decimal right first; the rest mostly fall out from a working Decimal implementation.

### Standard library inventory

The v1 stdlib layers, from lowest to highest:

- **SQL base types** (`Decimal`, `Integer`, `BigInt`, `Date`, `Timestamp`, `Varchar`, `Boolean`, `Json`, `Uuid`, `Array`): re-exports of sqlglot's `DataType` with convenience constructors.
- **Constraint primitives** (`PositiveInt`, `NonNegativeDecimal`, `BoundedFloat`): borrowed from `annotated-types` + Pydantic naming. Used via `Annotated[T, M]`, not class inheritance.
- **String formats** (`Email`, `Url`, `UUID`, `Hostname`, `IpAddress`): JSON Schema standard format names.
- **Refinement-axis enumerations** (`Currency`, `Country`, `LanguageTag`): ISO 4217 / 3166 / BCP 47 value sets, shipped as enums.
- **Analytics primitives** (`Money`, `Identifier`, `PrimaryKey`, `ForeignKey[target]`, `Count`, `Probability`, `Percentage`, `EventTime`, `LoadedAt`, audit columns): hand-written `DomainType` subclasses. Names and structure follow MetricFlow / DDD / dbt-utils precedent. This is where the real engineering goes.

Deferred to later: addresses, geo, quantities-with-units, phone numbers, domain-specific tax/jurisdiction types. Users declare those in their own project until a clear stdlib case emerges.

## The column proxy and expression builder

Contract methods work without a custom parser because of *column proxies*. `models.stg_order_items.subtotal` doesn't return a value; it returns a symbolic `Column` object that overloads arithmetic, comparison, and aggregation operators to build an AST.

This is the SQLAlchemy-Ibis-Polars idiom, well understood by data engineers and LLMs alike from heavy production use. Operations on column proxies return more proxies:

```python
col = models.stg_order_items.subtotal       # Column proxy
total = col.sum()                            # Aggregate proxy
per_order = total.group_by(                  # Grouped aggregate proxy
    models.stg_order_items.order_id
)
ratio = per_order / models.fct_orders.order_total.sum()
contract = ratio.within(0.01).relative_to(1.0)
```

A small set of operations covers the contract use cases:

- **Arithmetic.** `+`, `-`, `*`, `/`, `**`.
- **Comparison.** `==`, `!=`, `<`, `<=`, `>`, `>=`. These return `Predicate` objects rather than booleans.
- **Boolean combinators.** `&`, `|`, `~` (following the Polars and Pandas convention, since `and`/`or` can't be overloaded in Python).
- **Aggregations.** `.sum()`, `.count()`, `.count_distinct()`, `.min()`, `.max()`, `.avg()`. Return aggregate proxies.
- **Grouping.** `.group_by(col)`, `.group_by(col1, col2)`. Returns a grouped proxy whose subsequent aggregations are per-group.
- **Predicates and scoping.** `.is_null()`, `.is_not_null()`, `.in_(...)`, `.between(a, b)`.
- **Tolerance and comparison helpers.** `.within(eps)`, `.within(pct).relative_to(...)`, `.equals(...)`.
- **Structural predicates.** `models.X.exists()`, `models.X.has_column("name")`, `models.X.row_count()`. These are about project structure, evaluated at analyzer time.

This list is *finite by design*. The set grows only when a real contract can't be written in the current vocabulary. We use SQL syntax for SQL-shaped operations, like `.group_by(col)`, because familiarity compounds: every SQL idiom we preserve is one less thing readers have to learn and one less variation we have to maintain.

### The escape hatch

When the proxy API genuinely can't express a contract, typically because the predicate needs custom logic over the materialized data, a contract can drop into a runtime function:

```python
@contract
def stock_levels_consistent(df_orders, df_inventory, df_returns):
    """
    Per product, expected on-hand stock = initial - sold + returned.

    Takes materialized DataFrames rather than column proxies, because the
    relation spans three models with multi-step arithmetic the symbolic form
    doesn't express. The analyzer cannot read it, so it is verify-only: it
    receives Hegel-generated inputs and returns a boolean.
    """
    sold = df_orders.groupby('product_id')['quantity'].sum()
    returned = df_returns.groupby('product_id')['quantity'].sum()
    expected = (
        df_inventory.set_index('product_id')['initial_stock']
        - sold
        + returned.reindex(sold.index, fill_value=0)
    )
    actual = df_inventory.set_index('product_id')['on_hand']
    return (actual == expected).all()
```

The framework loses static analysis on these (it can't propagate types through arbitrary Python), and it can still run them inside the PBT loop and report failures with shrunk counterexamples. The escape hatch is meant to be rare and visible; a project with many materialized `@contract` declarations is signaling that the proxy API is missing a useful primitive that should be added. Prefer the symbolic form, a `@contract` returning a fact or a proxy predicate, when it fits; reach for the materialized form over DataFrames when the declarative shape doesn't capture the relation.

This is the same `@check` decorator pattern Pandera uses, applied at the contract layer.

## How declarations attach to dbt models

### Project layout

The package operates as a regular Python module, scanning a `dblect/` directory at the dbt project root by default. This is the pytest/conftest model: it works without configuration and is easy to override. An entry in `pyproject.toml` (or a `dblect.toml`) lets users point dblect at non-standard locations:

```toml
[tool.dblect]
declarations = ["dblect/", "shared_types/"]
manifest = "target/manifest.json"  # default
```

Suggested layout:

```
my_jaffle_project/
├── dbt_project.yml
├── models/
│   └── ... (existing dbt models, unchanged)
├── dblect/
│   ├── __init__.py           # empty; makes the directory importable
│   ├── types.py              # DomainType definitions
│   ├── flags.py              # DomainFlag declarations
│   ├── contracts/
│   │   ├── __init__.py
│   │   ├── staging.py        # ModelContract for staging models
│   │   ├── marts.py          # ModelContract for mart models
│   │   └── reconciliation.py # ContractGroups, standalone contracts
│   ├── fixtures.py           # Optional: custom fixture overrides
│   └── _stubs/
│       └── models.py         # auto-generated stubs for the `models` proxy
├── .dblect/                  # gitignored cache (counterexamples, parsed-manifest cache)
└── pyproject.toml            # dblect listed as a dev dependency
```

The dblect directory sits alongside dbt's, doesn't intrude on it, and is fully optional. `dblect init` lays down the skeleton (`__init__.py`, `types.py`, `contracts/`) with docstring-only starter files; users fill them in as they go. The `_stubs/` directory is autogenerated and gitignored.

A dbt project with no `dblect/` directory is a zero-declaration audit candidate, using only what the framework can infer from the manifest itself.

### Binding to dbt models

The `ModelContract` class declares its dbt model identifier as a class attribute, resolved against `manifest.json` using the same rules dbt itself uses for `{{ ref() }}`:

```python
class FctOrders(ModelContract):
    dbt_model = "fct_orders"                      # bare name; resolved like ref()
    # or
    dbt_model = "marts.fct_orders"                # path-qualified
    # or
    dbt_model = dblect.ref("fct_orders",          # explicit
                        package="my_jaffle_project")
```

Bare names are looked up in the local project first, then in installed packages; ambiguous names error and demand qualification. Internally everything gets normalized to dbt's `unique_id` format (`model.my_jaffle_project.fct_orders`) for keying registries and indexes. Users almost never see that form.

Registration happens via `__init_subclass__`. When the class definition is executed during the module scan, `ModelContract.__init_subclass__` runs, captures the class in a global registry, and queues it for resolution. Resolution against the manifest happens after the scan completes, so all classes are registered before any references are checked. Missing models, ambiguous references, and type errors surface as findings in the dblect report rather than as Python import errors. That separation matters: a typo in one contract file shouldn't prevent the rest of the project from being analyzed.

### Loading lifecycle

dblect consumes dbt's manifest as the single source of truth for project structure. Two integration patterns, both supported:

*Pattern A (convenient default).* `dblect audit` invokes `dbt parse` if the manifest is stale, reads the resulting `target/manifest.json`, then loads dblect declarations. One command from the user's perspective. Adds `dbt-core` as a Python dependency.

*Pattern B (explicit).* The user runs `dbt parse` (or any dbt command that writes a manifest) themselves; `dblect audit --manifest path/to/manifest.json` consumes the existing one. Uses `dbt-artifacts-parser` instead of full `dbt-core`. Fits well in CI environments where dbt and dblect run in separate stages.

The full load sequence:

```
parse manifest
  → import dblect modules (triggers __init_subclass__ registration)
  → merge schema.yml signals (see below)
  → resolve dbt_model bindings against manifest
  → resolve cross-references in contract ASTs
  → emit findings
```

### The `models` proxy

`models.stg_orders.subtotal` works without import-time errors via lazy attribute resolution. `dblect.models` is an object whose `__getattr__` returns a `ModelProxy(name)` for any name, and `ModelProxy.__getattr__` returns a `ColumnProxy(model_name, col_name)`. Nothing resolves at access time; the proxies are symbolic, captured into the contract's expression AST, and validated during the resolution phase.

This approach needs no code generation, with the tradeoff that editors offer limited autocomplete on the proxy because `__getattr__` returns a generic type.

For richer editor experience, the framework reads the manifest and writes a `dblect/_stubs/models.py` file with concrete class definitions. This happens automatically as part of `dblect init` and re-runs whenever the manifest changes (the next `dblect audit` or `dblect check` regenerates if stale):

```python
# auto-generated; do not edit
from dblect import ModelProxy, ColumnProxy

class _StgOrders(ModelProxy):
    order_id: ColumnProxy
    customer_id: ColumnProxy
    subtotal: ColumnProxy
    ordered_at: ColumnProxy

class _FctOrders(ModelProxy):
    order_id: ColumnProxy
    customer_id: ColumnProxy
    order_total: ColumnProxy
    tax_paid: ColumnProxy
    order_date: ColumnProxy

class _Models:
    stg_orders: _StgOrders
    fct_orders: _FctOrders
    # ...

models: _Models
```

The user imports `from dblect._stubs import models` instead of `from dblect import models` and gets autocomplete, type-checking, and refactor-rename. Stubs regenerate whenever the manifest changes, driven by the framework itself rather than by a separate manual command. Prisma's generated clients and dlt's source schemas use the same pattern.

Stubs are the standard path because the editor experience is the entire reason to use Python rather than strings. Lazy resolution remains available as a fallback for environments where build-time stub generation is unwelcome.

### Reading schema.yml for free signal

dbt's existing `schema.yml` already carries information dblect should consume directly, sparing users from restating it:

- **`tests: [unique, not_null, relationships]`** are primary keys and foreign keys in everything but name. The fixture builder honors them automatically. A `relationships` test pointing at another model's column *is* a foreign key declaration; users who've written one shouldn't have to also write `dblect.ForeignKey(...)` in Python.
- **`description` fields** are useful for documentation surface and for the LLM-assisted auto-suggestion path.
- **`meta` fields**, which dbt accepts under any node for downstream tooling, are the natural place to land the eventual YAML ergonomics extension. For v1 we can already read `meta.dblect.*` entries as a complementary declaration source.
- **Column `data_type`** is the base SQL type, used as a sanity check on domain-type declarations.
- **dbt model contracts** (the dbt feature, distinct from ours) provide structural type constraints. The two compose well: users who've used dbt's contracts get structural guarantees from dbt, and dblect adds the semantic layer on top.

The schema.yml read is one-directional in v1: dblect consumes and never writes back. The eventual YAML ergonomics extension would change that, and it's deferred.

### The reverse direction

A user asking "what does dblect know about this model?" gets a single dict lookup against a reverse index built during resolution:

```
$ dblect inspect fct_orders
Model: marts.fct_orders
  Contract class: FctOrders (dblect/contracts/marts.py:42)
  Columns:
    order_id: OrderId
    customer_id: ForeignKey(dim_customers.customer_id)
    order_date: OrderDate
    order_total: RevenueNet [ge=0]
    tax_paid: TaxAmount [ge=0]
  Contracts:
    order_total_matches_line_items [conservation, tolerance=0.01]
    one_row_per_order [cardinality, 1:1 on order_id]
  Referenced by contracts in:
    ReturnReconciliation.refunds_never_exceed_originals (dblect/contracts/reconciliation.py:18)
  Flag worlds affecting this model:
    include_tax_in_revenue: {True, False}
```

This index is also what the MCP server exposes.

## Flags and switches

dbt's mechanism for varying behavior across runs is the `var()` system. `{% if var('include_tax_in_revenue') %}` and `{{ var('start_date') }}` resolve from `dbt_project.yml`, command-line arguments, or environment variables. Real projects use this to implement what they call feature flags, and the design works well for dbt's needs. dblect's job is to add type awareness on top: find the var references, classify their domains, and reason about their effects on column types and downstream contracts.

### Discovery: walking the Jinja

dbt's `manifest.json` includes both `raw_code` (Jinja-laden source) and `compiled_code` (resolved against one specific var set). For flag discovery we use `raw_code`.

The discovery pass parses each model's `raw_code` with `jinja2.Environment().parse()`, producing a Jinja AST with `If` nodes, `Call` nodes, and `Name` nodes. It walks the AST collecting every `var()` and `env_var()` call along with its arguments, defaults, and surrounding context (inside `{% if %}`, inside `{{ }}`, inside a `WHERE` clause, inside `config()`, and so on).

Macros are the wrinkle. Many projects wrap flag logic in user-defined macros: `{{ if_feature('returns_v2', 'new', 'old') }}` where `if_feature` internally calls `var('feature_returns_v2')`. dbt's manifest includes macro source under its `macros` section, so dblect builds a "what vars does this macro touch" index per macro and propagates references back to call sites. This preserves attribution: the framework knows a given var reference came from `if_feature` with a specific argument, which matters for naming and reporting.

After this pass we have a complete catalog: every var reference, where it appears, what default it has, and what context it's used in.

### Switch vs parameter: a real distinction

Flags split into two categories the framework treats differently.

**Switch flags** have small finite domains and get enumerated by the type system. Every downstream type is checked under every flag value. Examples in a jaffle context: `include_tax_in_revenue` (boolean), `include_returns_in_revenue` (boolean), `revenue_basis` (enum `["accrual", "cash"]`), `multi_currency_mode` (enum). A real project usually has five to fifteen of these.

**Parameter flags** have continuous or large domains and get treated as input generators by Hegel. The runtime PBT loop samples values from the declared range; the type system doesn't enumerate them. Examples: `start_date` (a date range), `batch_size` (an integer), `historical_window_days` (a duration), `source_schema` (a string identifier). The long tail.

The user makes this call per flag because the type system would explode if every var were enumerated, and it would miss real bugs if every var were treated as opaque. Switch flags are where the type-propagation story earns its keep; parameter flags fold into the existing Hegel input-generation story.

### Scaffolding: the discovery-to-declaration handoff

dblect can't infer domains soundly, because dbt vars are intentionally flexible by design and there's no project-level type system for them. The framework can do most of the work and hand the rest to the user. The discovery pass produces draft `DomainFlag` classes (one per var) with type, domain, and default pre-filled where they could be inferred, and the `affects` clause left for the user to write. See [Flags and configuration in dblect](flags_and_configs_as_types.md) for the full scaffolding behavior, the inference heuristics, and the `DomainFlag` declaration shape that's canonical going forward.

The heuristics (briefly): used only inside `{% if var(...) %}` → boolean candidate; comparison against string literals → enum candidate with observed literals; date or time arithmetic context → date or interval parameter; listed in `dbt_project.yml`'s `vars:` block with a default → type inferred from the default; no detectable context → user must classify. This is the "scaffold and refine" pattern Pandera uses for schema inference, applied here to a domain it wasn't originally designed for. Anything left unclassified surfaces as a soft finding rather than blocking analysis.

### Enumeration in analysis

Two strategies, used for different parts of the pipeline.

*Branching AST, used for type propagation.* Build a representation where each `{% if var(...) %}` becomes a branch node, and propagate types through both branches simultaneously. The output type of an expression that depends on a switch flag *is* a switch type. Implementation: preprocess the Jinja so each branch becomes a sqlglot-parseable variant with a marker, parse all variants of a model, weave them back together as a typed-with-branches AST. One pass gives results for all worlds at once, which matches the design philosophy.

This works for type propagation. Runtime checks need actual SQL strings to feed to DuckDB, so they use the second strategy.

*Re-render under each flag assignment, used for runtime PBT.* For runtime checks, enumerate switch combinations, re-render the Jinja under each assignment, get N concrete SQL strings, and run each. Two ways to render: invoke `dbt parse --vars '{"include_tax_in_revenue": true}'` as a subprocess (highest fidelity, slowest), or render the Jinja directly with a stripped-down context (faster, fragile against models that use adapter-specific macros). Default to the subprocess invocation; offer the lighter path as an opt-in fast mode for local iteration.

For a project with 5 switch flags of domain sizes [2, 2, 3, 2, 2], there are 48 worlds. Most contracts only touch a couple of flags. The framework is lazy about enumeration: for each contract, identify which switch flags actually affect it (via the reference graph from discovery), and enumerate only that subspace. The "many flags, many contracts" case stays tractable.

### Worked example

Consider a jaffle model that branches on the tax flag:

```sql
-- models/marts/fct_orders.sql
SELECT
  order_id,
  customer_id,
  ordered_at,
  {% if var('include_tax_in_revenue') %}
    subtotal + tax_amount AS revenue
  {% else %}
    subtotal AS revenue
  {% endif %}
FROM {{ ref('stg_orders') }}
WHERE ordered_at >= '{{ var("start_date") }}'
```

After the discovery pass and user refinement, a `DomainFlag` class for `include_tax_in_revenue` declares its `affects` clause as `RefinementEffect(target=Revenue.contains_tax, value_when_true=True, value_when_false=False)`. The framework enumerates flag worlds and propagates:

- World `{include_tax_in_revenue: True}`: `fct_orders.revenue` has type `Revenue(contains_tax=True, contains_discount=True)`, assuming `subtotal` is post-discount.
- World `{include_tax_in_revenue: False}`: `fct_orders.revenue` has type `Revenue(contains_tax=False, contains_discount=True)`, which is `RevenueNet`.

A downstream model `marts.discounts` declares its input as `RevenueNet`:

- World `{include_tax_in_revenue: False}`: types align.
- World `{include_tax_in_revenue: True}`: type mismatch, since `Revenue(contains_tax=True)` doesn't unify with `RevenueNet`.

Finding emitted at PR time:

```
discounts.amount_in [type mismatch under flag world]
  Expected: Revenue(contains_tax=False, contains_discount=True)
  Got:      Revenue(contains_tax=True,  contains_discount=True)
  Under flag: include_tax_in_revenue = True
  Path: stg_orders.subtotal → fct_orders.revenue → discounts.amount_in
```

Runtime PBT covers the orthogonal case where types align and values don't. Hegel runs the chain under each switch world, with `start_date` and underlying data generated per run. Contracts are checked in each world independently; failures are tagged with the world they occurred in.

### Two complications worth flagging

*Vars that depend on other vars.* Projects sometimes do `{% set effective_window = var('attribution_window') if var('use_short_window') else var('long_window') %}`, with vars referenced inside Jinja set blocks to compute derived values. The discovery pass handles this (jinja2's parser does too), and the reference graph needs to model the derivation. For domain inference this gets tricky, and the user usually declares the derived value's domain explicitly.

*Vars in `config()` calls.* `{{ config(materialized='incremental' if var('full_refresh_mode') == 'no' else 'table') }}` introduces flags affecting the model's materialization rather than its schema. dblect detects these and routes them to a different concern lane: they affect replay-determinism and incremental contracts rather than column types. The discovery pass flags them separately.

### CLI surface

Flag discovery and the initial draft `DomainFlag` classes are produced by `dblect init` as part of its end-to-end first-run flow. Dedicated flag subcommands (re-scaffold on demand, list, world enumeration, impact analysis) are not in the v1 surface. `dblect init`'s idempotent re-run covers the re-scaffold case, and per-world selection is a CLI flag on `dblect check`:

```
dblect check --flag-world include_tax_in_revenue=true,revenue_basis=cash
```

A dedicated `dblect impact --flag X` command ("if I flip this flag, what could break?") is the most likely first addition once the v1 surface settles. The implementation is a single graph query over the flag references plus the declared contract dependencies. The original pitch ("make flag flips reviewable the same way code changes are") is what this command makes operational; it gets a real CLI verb once the registry it queries is stable.

## How declarations become property-based tests

The translation from declaration to verification is mechanical, in the same way Pandera's `schema.strategy()` is mechanical.

- A domain type → a generator. `RevenueNet` becomes a generator over positive decimals in the relevant currency context.
- A `ModelContract` → a stateful fixture rule that produces rows for that model, respecting declared foreign keys to other models' rules.
- A contract method → a property over the fixture state.
- Custom runtime contracts → Python predicate functions called inside the framework's invariant-checking loop.
- Parameter flags → input generators sampled per run.
- Switch flags → outer enumeration loop over the relevant flag subspace.
- Shrinking is FK-aware, a settled constraint since naive shrinking orphans children.
- Generation is contract-directed: each contract runs under the intents in the v1 catalog (Fanout, Orphan, NullKey, EmptyGroup, OrderingTie, ReplayShuffle, Duplicate, LateRow, Boundary), so structural failure shapes get probed deliberately rather than discovered by chance. See [contract-directed-generation.md](contract-directed-generation.md) for the catalog and architecture.

The framework's compilation step walks the registry of declared types, models, contracts, and flags, builds the dependency graph, and emits a PBT suite that the CLI runs. Users don't write `@given`. They write classes and decorators; the PBT engine runs in the basement.

The underlying engine is Hypothesis in v1. Hegel (a Hypothesis-descended library aimed at the stateful and multi-table cases dblect needs) is the longer-term target; if and when it's available as a callable dependency, the swap is mechanical because the conceptual vocabulary transfers without modification.

## Static analysis on Python declarations

Python contracts are introspectable. The framework loads dblect modules, inspects the registered classes, and walks the AST each contract method built. From there:

- **Type propagation.** Given column annotations on all models, the static analyzer propagates types along dbt-DAG edges using sqlglot's column-level lineage on the underlying compiled SQL. When a column's declared type doesn't match the inferred type from upstream, that's a finding.
- **Change-impact at PR time.** When a contract method's AST references `models.stg_orders.subtotal`, the framework records a dependency. A PR retyping `stg_orders.subtotal` enumerates all contracts referencing it and re-checks them.
- **Type-level contract checking.** Many contracts can be settled statically without running. `sum(RevenueGross) == sum(RevenueNet)` is a type error at the comparison node; no PBT run needed.
- **Switch-type enumeration.** Flag-conditional types are enumerated; every world is type-checked; findings include the flag value(s) under which the violation occurs.

The Python AST that contract methods build isn't sqlglot's AST. It's dblect's internal `Expression` AST, structured around column proxies and the operations they support. The framework converts to and from sqlglot when needed (compiling to DuckDB-executable SQL for the runtime verification path; consuming sqlglot lineage on user dbt SQL for the propagation path), and the internal representation is its own.

## YAML as ergonomics extension (deferred)

A reasonable future extension is to allow the simplest declarations, like typed columns on staging models and basic cardinality contracts, to be authored in YAML inside dbt's existing `schema.yml` rather than as Python classes. The schema would be Pydantic-validated and would produce the same internal AST as the Python equivalent. This is genuinely useful for analytics engineers more comfortable in YAML than Python, and it composes well with dbt's existing YAML conventions and `meta:` extension points.

We're not doing this in v1. The reasons:

- The Python surface has to exist anyway, for everything beyond trivial cases. Building it first establishes the AST and the contract registry. The YAML loader can be added later as a second producer of the same AST.
- Mixing two surfaces from day one risks them diverging, with features added in one but not the other, or semantic mismatches at the boundary.
- The most valuable cases (complex preconditions, custom predicates, cross-model contracts, switch types) want the Python surface. Optimizing the simple cases first inverts the priority.

When we revisit this, the model is Pydantic itself. Pydantic models can be constructed from dicts (and thus from YAML), and the dict form produces the same validated object. Same shape here.

## Influences and what we borrow

This section accounts for the prior art dblect draws on. It isn't exhaustive; it focuses on libraries whose ideas shape concrete decisions in the design. The intent is to credit the lineage and help readers locate dblect mentally against tools they may already know.

### Pydantic

Pydantic established the modern Python pattern of declarations as classes with type-annotated fields and `Field(...)` metadata, where the class serves as both Python code (for IDE tooling, autocomplete, refactoring) and structured data (for JSON schema export, validation, serialization). The pattern is now so widely adopted that an entire ecosystem of libraries assumes it.

dblect adopts the Pydantic class-and-field pattern directly. `DomainType`, `ModelContract`, `ContractGroup`, and the flag declarations are all Pydantic-shaped classes whose fields carry type annotations and `Field(...)` metadata. The class is both the authoring surface for users and the data structure the framework introspects, with no separate config file or schema document required.

What dblect takes specifically: the class-as-declaration pattern, `Field(...)` metadata for per-field constraints, methods on declaration classes for derived computation, the JSON-schema export idea (we want similar for the MCP surface so Claude can inspect contracts as JSON), and the broader convention that declaration objects are the primary interface rather than configuration files.

### Hypothesis (and Hegel as the longer-term target)

Hypothesis is the canonical Python property-based testing library, with a years-long track record and the most refined shrinking implementation in the Python ecosystem. Users write `@given(strategies.integers())` and the framework synthesizes adversarial inputs, shrinks counterexamples to minimal forms, remembers failing examples across runs, and biases generation toward known edge cases. dblect's v1 PBT layer is built on Hypothesis directly.

Hegel descends from Hypothesis (same authors, same core engine, same shrinking philosophy) with extensions for the stateful and multi-table scenarios PBT against data pipelines particularly wants. dblect treats Hegel as the longer-term target: if and when it's available as a callable Python dependency, the swap from Hypothesis to Hegel is mechanical because the conceptual vocabulary is the same. Until then, the v1 build uses Hypothesis with dblect-side machinery filling in the FK-aware fixture and multi-table shrinking pieces.

What dblect takes specifically: the entire mental model of generators-as-types, the shrinking-to-minimal-counterexample workflow (made FK-aware for multi-table fixtures), the idea that the framework picks generators automatically from type information, and the `@given` pattern itself, even though dblect hides it from users behind the `@contract` marker. The dblect user never types `@given`; the framework supplies it.

### SQLAlchemy

SQLAlchemy introduced the column-proxy idiom that the entire Python data-tools ecosystem now uses. A SQLAlchemy `Column` object is a symbolic Python value: `User.age == 30` produces a `BinaryExpression` rather than a boolean, allowing the library to capture query intent as an AST that compiles to SQL at execution time. The pattern is foundational enough that it now appears in Pandas, Polars, Ibis, dlt, Apache Beam, and most modern Python data libraries.

dblect's column proxy mechanism is a direct application of this pattern. `models.stg_orders.subtotal == 100` returns a `Predicate`; `.sum().group_by(col)` returns an `AggregateExpression`; contract method bodies build an AST rather than executing. The two-stage architecture (build expression, then compile to backend) is also SQLAlchemy's: dblect compiles expressions to DuckDB SQL or to a PBT property function depending on the verification path.

What dblect takes specifically: the column-as-symbol pattern, operator overloading for AST construction, lazy expressions that don't evaluate until compiled, the separation between expression building and execution, and the general design discipline of treating "Python that builds a query plan" as a first-class API surface.

### Ibis and Polars

Ibis and Polars are modern reimaginings of SQLAlchemy's column-proxy idea, with cleaner method-chain APIs and stronger separation between expression and execution. In both, `df.column.sum().group_by(df.other)` builds a query plan that compiles to one of several backends (DuckDB, BigQuery, Postgres, or Polars's own engine). The style reads like data pipelines and stays closer to the way analysts think about data than the older ORM-flavored SQLAlchemy Query API.

dblect's contract expression API reads more like Ibis or Polars than like SQLAlchemy's classic ORM. Method chains, `.sum().group_by()`, `.within().relative_to()`, lazy frame semantics, multi-backend compilation, all of these are direct stylistic borrowings.

What dblect takes specifically: the contemporary method-chain idiom for expression building, the lazy-frame mental model, and the convention that aggregations and groupings are methods on column proxies rather than free functions.

### Pandera

Pandera applies the Pydantic class-based pattern to DataFrame schemas. The result is the closest precedent for the schema-is-generator architectural move dblect needs. The crucial feature is `schema.strategy()`, which returns a Hypothesis strategy from a schema declaration, unifying the declaration and the generator into a single object. Pandera also pioneered the `@check` decorator pattern for custom predicates the declarative system can't express, lazy validation as a first-class mode (collecting all errors rather than failing on the first), and an `example()` method for materializing schema instances.

dblect's `MyContract.fixture()` is the direct descendant of Pandera's `MySchema.strategy()`, scaled up for FK-respecting multi-table generation. The materialized-frame escape hatch follows Pandera's `@check` pattern directly. Lazy validation is the default mode in dblect, just as it is in Pandera. The "scaffold and refine" workflow for flag discovery is modeled on Pandera's schema inference workflow.

What dblect takes specifically: the schema-yields-a-generator unification (this is the single most important pattern we take from Pandera), lazy validation as the default mode, the `@check` escape hatch for predicates the declarative system can't express, the `example()` method for materialization, and the scaffold-and-refine workflow for inferring declarations from existing artifacts.

dblect borrows patterns from Pandera gratefully, without depending on it as a library. Pandera is built on pandas, which is the right foundation for its target users. dblect operates on warehouse SQL through dbt-duckdb, so we adapt the patterns to that substrate rather than inherit Pandera's dependency surface.

### mypy and pyright

mypy and pyright are the canonical Python static type checkers, and they established the user-experience pattern dblect's static analyzer follows. Users declare types in source code; a separate analyzer walks the AST at check time; findings emit as structured diagnostics rather than runtime crashes; PR-time checks run on changed files and propagate impact to affected dependents; type propagation follows data-flow edges; findings have a standardized output format that downstream tools (CI runners, editor diagnostics, code-review bots) can consume.

dblect's analyzer is, structurally, a type checker for SQL data. The dbt DAG is the data-flow graph; domain types are the types; the analyzer walks contract ASTs and column-lineage edges; findings emit in a format CI tools and the MCP surface can consume. The architectural inheritance from mypy and pyright is direct, even though the type system itself is quite different (semantic refinements on SQL columns rather than Python types).

What dblect takes specifically: the user-declares-types pattern, the separate-analyzer model, structured findings rather than crashes, PR-time impact propagation, the type-error-finding output format, and the general design principle that static checking is a separate phase that complements rather than replaces runtime verification.

### MetricFlow and the dbt semantic layer

MetricFlow is the dbt-native semantic layer, where users declare entities, dimensions, and metrics in YAML and the system generates SQL for metric queries. It's the closest ecosystem peer to dblect: it works in the same project space (typed declarations on top of dbt), with similar architectural commitments (declarations as data, Python API alongside YAML, generated artifacts from declared sources).

dblect operates at a different layer. MetricFlow specifies what a metric is and how to compute it. dblect specifies what a column means and what invariants it satisfies. The two compose well. A MetricFlow metric definition might say "weekly_revenue is sum(order_total) grouped by week"; a dblect declaration might say "order_total is RevenueNet and conserves with line item subtotals." The MetricFlow definition tells you the metric; the dblect declaration tells you the meaning and the guarantees.

What dblect acknowledges: the dbt-native approach to semantic specification, the YAML-and-Python coexistence pattern (which we're deferring to v2 but inheriting the shape of), and the general principle that semantic information about dbt models is worth declaring as a first-class artifact alongside the model SQL.

### Prisma and dlt

Prisma (TypeScript ORM) and dlt (Python data ingestion) both follow a "schema as source of truth, generated typed client as artifact" pattern. The user declares a schema in a structured form, runs a generation step, and gets back an autocomplete-rich, type-safe client. Schema changes trigger client regeneration through a CLI command or build hook.

dblect produces a generated `models.py` from the dbt manifest (written to `dblect/_stubs/models.py` as part of `dblect init` and refreshed automatically when the manifest changes), with concrete class definitions for each dbt model that give users autocomplete, type-checking, and refactor-rename in their editor. The pattern and the workflow are directly from this lineage.

What dblect takes specifically: the generated-typed-client pattern, the regenerate-after-schema-change workflow, the convention of placing generated files in a separate package so they don't get committed-and-reviewed as if they were authored, and the broader idea that the developer experience of working with a declared schema deserves first-class engineering attention.

### pytest

pytest established the `conftest.py` discovery pattern that dblect uses for declaration scanning. The framework recursively scans a directory, imports every Python module it finds, and relies on side effects of imports (class definitions, decorator applications) to register fixtures, tests, and plugins. No explicit registration file is required.

dblect uses the same pattern. Scan `dblect/`, import every module, rely on `__init_subclass__` and decorator side effects to populate the registry. The discovery model is configurable via `pyproject.toml` the same way pytest is configurable via `pytest.ini` or `pyproject.toml`. The conventions transfer almost without modification.

What dblect takes specifically: the auto-discovery pattern with override capability, the side-effect-of-import registration model, the convention of placing tool configuration in `pyproject.toml`, and the broader principle that a useful tool should work without configuration in the common case and accept configuration only when the user's setup deviates from convention.

### sqlglot

sqlglot is a SQL parser, transpiler, and analysis library covering more than twenty dialects. It builds an AST from SQL strings, supports cross-dialect translation, provides column-level lineage through compiled SQL, and exposes a type system internal to its expression model. SQLMesh is built on it, dbt-core uses it internally for some analysis, and it has become the default Python SQL substrate over the past few years.

dblect's relationship with sqlglot is different in kind from the relationships above. The other libraries in this section influence dblect's API shape or architectural choices. sqlglot is core machinery: dblect depends on it to function. The static analyzer walks sqlglot ASTs to detect ordering hazards. The column-lineage engine produces type propagation paths through compiled dbt SQL. `dblect.types` re-exports sqlglot's `DataType` directly rather than wrapping it; generators and coercion rules live in free-function dispatch tables keyed by sqlglot type. The runtime verification path renders dblect expressions to dialect-appropriate SQL via sqlglot. Where sqlglot has good support for something, dblect uses it directly; where sqlglot is thin, dblect builds on top.

What dblect uses specifically: AST parsing for dbt's compiled SQL, the `DataType` vocabulary as `dblect.types`' SQL type layer, column-level lineage as the foundation for type propagation, dialect-aware SQL rendering for the runtime verification path, and the general design of "SQL as a first-class Python data structure that can be analyzed and transformed."

The relationship is closer to the relationship between mypy and Python's `ast` module than to the relationship between dblect and Pandera. Foundational machinery rather than a peer library whose patterns we emulate.

## Open questions

A number of earlier open questions are resolved in [questions_and_decisions.md](questions_and_decisions.md): the DSL shape (Style A, decorated methods on Pydantic-shaped classes), DomainType structure (scalar, B1 syntax), flag/type composition direction (flags know the type), the namespace (`dblect.Field`, `dblect.ForeignKey`, `dblect.flag`), the v1 generator scope (intents + synthesis, no mutation), audit scope (includes execution), the `dblect init` end-to-end flow, the ignore syntax (`# noqa-fixture`), and the v1 intent catalog. What remains genuinely unsettled:

- **AST shape.** The internal expression AST needs to be expressive enough to handle every operation the proxy API exposes, and restricted enough to be statically analyzable. The right starting point is probably "sqlglot AST with our extensions," and the proxy API doesn't have to produce sqlglot directly. It could produce its own AST that converts to sqlglot for the SQL-compilation path. Unsettled which is cleaner.
- **Sync vs async contract registration.** Pydantic and Pandera classes register on definition. For projects with hundreds of model contracts, eager import-time registration may be slow. Lazy registration via `dblect.scan(path)` is the alternative.
- **Macro expansion fidelity for flag discovery.** Recursive expansion is what dbt does internally. For our purposes we want reference tracking without full expansion, so that we don't diverge from dbt's compiled output. The right abstraction is probably a "macro shape" representation that records what vars each macro touches without materializing every expansion.
- **Vars-that-set-vars.** Real projects have `{% set x = var('y') if var('z') else var('w') %}`. Discovery handles this syntactically; domain inference for derived values requires user declaration. Workflow for getting users to declare these cleanly is unsettled.
- **Switch-type convenience vs DomainFlag canonical surface.** The canonical flag composition is the `DomainFlag` class with `affects = RefinementEffect(...)`. A `Revenue.switch(on=flag, cases={...})` shorthand may ship as a thin convenience over the same registry if the engineering cost is small; whether to bother is unsettled.
- **Direct Pandera compatibility (defer).** Could a dblect domain type be consumed as a Pandera column type and vice versa? Possibly useful for teams already on Pandera; possibly a tar pit.

None of these block a working v1. The right time to settle each is when a real contract or real user request forces the question.
