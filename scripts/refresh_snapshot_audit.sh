#!/usr/bin/env bash
# Regenerate the committed manifest.json for the snapshot-audit fixture.
#
# The fixture is a tiny dbt project with two snapshots (one using default validity
# column names, one renaming them via snapshot_meta_column_names) and consumer
# models that read them safely and unsafely. It backs the end-to-end test that the
# snapshot temporal-filter detector fires on real dbt-compiled SQL. Like the
# scenarios, it compiles a copy in a temp dir (so the committed manifest carries no
# repo path) and the committed manifest means tests need no dbt at run time.
#
# Usage:
#   scripts/refresh_snapshot_audit.sh
#
# Assumes ../jaffle_shop_duckdb is checked out next to dblect (it provides
# dbt-duckdb via its own uv environment), matching the other refresh scripts.

set -euo pipefail

DBLECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIXTURE="${DBLECT_ROOT}/tests/fixtures/snapshot_audit"
JAFFLE="${DBLECT_JAFFLE_DIR:-$(cd "${DBLECT_ROOT}/.." && pwd)/jaffle_shop_duckdb}"

if [[ ! -d "${JAFFLE}" ]]; then
  echo "error: ${JAFFLE} not found (set DBLECT_JAFFLE_DIR to override)." >&2
  echo "       Clone https://github.com/dbt-labs/jaffle_shop_duckdb next to dblect/." >&2
  exit 1
fi

tmp="$(mktemp -d "${TMPDIR:-/tmp}/dblect-snapshot-audit.XXXXXX")"
trap 'rm -rf "${tmp}"' EXIT

# Copy the project sources (not the committed manifest) into the temp build dir.
cp -R "${FIXTURE}/dbt_project.yml" "${FIXTURE}/models" "${FIXTURE}/seeds" \
  "${FIXTURE}/snapshots" "${tmp}/"

cat >"${tmp}/profiles.yml" <<YAML
jaffle_shop:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: ${tmp}/warehouse.duckdb
      threads: 2
YAML

run_dbt() {
  (
    cd "${JAFFLE}"
    if command -v uv >/dev/null 2>&1 && [[ -f pyproject.toml ]]; then
      uv run dbt "$@" --project-dir "${tmp}" --profiles-dir "${tmp}"
    elif command -v dbt >/dev/null 2>&1; then
      dbt "$@" --project-dir "${tmp}" --profiles-dir "${tmp}"
    else
      echo "error: neither 'uv' nor 'dbt' is available in ${JAFFLE}." >&2
      exit 1
    fi
  )
}

# Seeds must load so ref('raw_orders') resolves; compile renders every model and
# snapshot into the manifest (snapshots need not be built for the relation names
# and configs to land).
run_dbt seed
run_dbt compile

cp "${tmp}/target/manifest.json" "${FIXTURE}/manifest.json"
echo "Wrote ${FIXTURE}/manifest.json ($(wc -c < "${FIXTURE}/manifest.json") bytes)"
