# Var inference: discovery inputs

Status: design
Audience: engineers wiring the manifest macro registry and the project-config reader
Part of: [the var-inference plan](./plan.md)

This stream covers the two external surfaces the analysis reads before it walks anything: the macro registry that [macro-following](./macro-following.md) expands, and the project configuration that supplies var defaults and target overrides to [inference](./inference-and-classification.md). Both are plumbing, both are small, and both are isolated enough to land and test on their own.

## The manifest macro registry

`var()` calls are reached through macros, so following them needs every macro's source body keyed by name. dbt's `manifest.json` carries this: a probe of the jaffle fixture found its macros under the top-level `macros` key, each entry exposing `name`, `package_name`, `macro_sql` (the source body), `depends_on`, and `unique_id`. The current manifest reader ([`manifest/parse.py`](../../../src/dblect/manifest/parse.py)) parses `nodes` and `sources` into the typed `Node` view but does not surface macros at all.

### Design

Add a typed `Macro` to the manifest view and a name-indexed registry on `Manifest`:

```python
@dataclass(frozen=True, slots=True)
class Macro:
    unique_id: str
    name: str
    package_name: str
    macro_sql: str
    depends_on_macros: frozenset[str] = frozenset()
```

`Manifest` gains a `macros: Mapping[str, Macro]` populated from `parsed.macros` in `from_raw`, alongside the existing node and source population. A lookup helper resolves a macro name to its definition, with the package-qualification rule dbt uses (a bare name resolves within the project first, then packages; a `package.name` reference resolves directly). The registry is the input macro-following consumes; this stream owns producing it, not walking it.

### Why it is separable

It touches one file, adds a dataclass and a mapping, and changes no existing behavior. It can ship ahead of the walker. Its contract is a faithful transcription of the manifest's macro block into the typed view.

### Testing

- A round-trip test against the fixture manifest asserting the registry has an entry per macro in the raw JSON, with `macro_sql` non-empty for the project's own macros.
- A name-resolution test pinning the bare-name-then-package lookup order and the `package.name` direct path.

## The project configuration reader

Var defaults and target overrides live outside the manifest, in the project's YAML. The spec sources a variable's default from `dbt_project.yml` (declared vars and their defaults) and its target-specific values from `profiles.yml`. These feed inference as additional evidence: a boolean default with no contrary usage infers `bool`, and declared or target values are added to the inferred domain as observed members.

### Design

A small reader that, given the project directory, loads:

- `dbt_project.yml` `vars:` block, yielding declared var names and their default values. dbt allows both a flat `vars:` map and a per-project-name nesting; the reader normalizes both to a name-to-default map.
- `profiles.yml` (when present and readable) target-specific var overrides, yielding a per-target name-to-value map and the active target name.

The reader returns a typed `ProjectConfig` carrying the declared defaults, the per-target overrides, and the active adapter and target (the adapter also drives `adapter.dispatch` resolution in macro-following). It is total and degrades quietly: a missing or unreadable `profiles.yml` yields no target overrides rather than an error, matching the spec's posture that profile cross-reference is best-effort.

### The inline-default boundary

dbt vars can carry a default at the call site, `var(name, default)`, rather than in `dbt_project.yml`. The compiled SQL has already folded that inline default in, so the base world is correct as it stands, but the project-config reader does not see it: it reads YAML, not call sites. The Jinja walker does see it (the second `Const` argument on the `Call`), so the inline default is recoverable from the [front end](./jinja-frontend.md) and is the natural place to capture it. Until it is consumed, a var whose only default is inline is discovered and typed but carries no recorded default, and degrades to its single compiled value as one world, the same degrade-not-lie the computed case takes. This boundary is noted in [`config-and-flag-worlds.md`](../config-and-flag-worlds.md) under "Dependency on var-inference".

### Testing

- A reader test against a fixture `dbt_project.yml` with a `vars:` block in both the flat and nested shapes, asserting the normalized default map.
- A test that a missing `profiles.yml` yields empty target overrides without raising.

## Fixtures this stream needs

The current fixtures declare no vars, so this stream introduces small dbt-project fixtures whose `dbt_project.yml` declares vars with defaults (boolean, enum-like string, numeric) and whose `profiles.yml` carries target overrides, paired with model SQL that uses them. These fixtures are shared with the inference and scaffold streams.

## Open questions

- **Per-package vars.** Some vars are declared by installed packages. The spec leans toward filtering these out as not the user's vars; this reader can carry the package origin so the filter is a downstream choice rather than baked in here.
- **Declared-but-unused vars.** Whether a var declared in `dbt_project.yml` with no usage still produces a scaffold entry. This stream surfaces the declaration; the [scaffold stream](./scaffold-and-cli.md) decides whether to emit it.

## References

- [The var-inference plan](./plan.md) and [the original spec](../var-inference-spec.md), "Inputs" and "Discovery".
- The macro registry's consumer: [macro-following](./macro-following.md).
- The defaults' consumer and the inline-default boundary: [inference and classification](./inference-and-classification.md), [`config-and-flag-worlds.md`](../config-and-flag-worlds.md).
</content>
