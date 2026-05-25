# dblect docs

Three buckets, each answering a different question.

## What dblect *is*: vision

- [**dblect-overview.md**](dblect-overview.md): the elevator pitch. What dblect adds on top of dbt, what class of bug it targets, where it sits next to existing tooling.

## What's *built* today: current state

- [**current_state/architecture.md**](current_state/architecture.md): a walkthrough of the code as it stands: manifest ingestion (including dbt tests + constraints) → SQL parsing → structural detectors → uniqueness facts (declarations + structural proof) and the uniqueness-aware detector → audit walker → CLI + reporters. Reads bottom-up from a fresh checkout. Start here if you want to navigate the source or extend a detector.

## What's *designed but not built*: forward-looking

Working design notes for the layers above the current static analyzer. These describe the intended shape; the code that implements them mostly doesn't exist yet.

- [**design/dblect_technical_intro.md**](design/dblect_technical_intro.md): the typed contracts and semantic-types DSL. Style A (decorated methods on `ModelContract` subclasses), the type registry, refinements, flag-conditional types.
- [**design/tiers_and_rough_implementation_order.md**](design/tiers_and_rough_implementation_order.md): the three tiers of developer investment (audit / semantic types / focused contracts) and the implementation sequence across them.
- [**design/design-concepts-digest.md**](design/design-concepts-digest.md): the structural-vs-domain lattice, propagation rules, and other architectural concepts that span the design.
- [**design/contract-directed-generation.md**](design/contract-directed-generation.md): the generator architecture: intent catalog, multi-table coordinated generation, shrinking.
- [**design/flags_and_configs_as_types.md**](design/flags_and_configs_as_types.md): how dbt vars and env_vars become typed configuration, the `SemanticFlag` shape, world enumeration, PR-time flag-flip analysis.
- [**design/var-inference-spec.md**](design/var-inference-spec.md): implementation spec for the var-discovery and inference pass that populates `SemanticFlag` scaffolding.
- [**design/column-level-lineage.md**](design/column-level-lineage.md): column-level lineage and property propagation built on K-relations (Green-Karvounarakis-Tannen 2007) marrying with `sqlglot.lineage`. One propagation engine; properties (uniqueness, fanout, nullability, semantic tags) instantiate it by choosing a semiring and per-operator transfer functions. Closes the multi-source uniqueness gap and is the substrate the semantic-types layer will tag-propagate over.
- [**design/demo_walkthrough.md**](design/demo_walkthrough.md): the canonical end-to-end demo against `jaffle_shop_duckdb`. Output and commands are illustrative; the demo presupposes the typed-contract layer.

## Other docs at the repo root

- [`../questions_and_decisions.md`](../questions_and_decisions.md): the decisions log. Questions raised during design and how they were resolved.
- [`../CLAUDE.md`](../CLAUDE.md): code-style and prose rules the project follows.
- [`../CHANGELOG.md`](../CHANGELOG.md): user-facing changes.
