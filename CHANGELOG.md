# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial project scaffolding: `pyproject.toml` (hatchling, ruff, pyright strict, pytest), `src/dblect/` layout, CI workflow, README, vendored jaffle test fixture, manifest ingestion module.

### Changed
- `aggregation_not_well_typed` findings now name what the coherence guard reasoned about: the aggregate and the column it reduced, the per-row companion that is not held constant, and the grouping that fails to hold it, instead of a generic message (#109).

[Unreleased]: https://github.com/dvryaboy/dblect/commits/main
