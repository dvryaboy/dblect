#!/usr/bin/env bash
# Regenerate the committed manifest.json for each demo scenario.
#
# Each scenario is a thin overlay on a shared currency-aware jaffle base. This
# script composes base + overlay into a temp dbt project, runs `dbt compile`
# through the jaffle_shop_duckdb environment (so dblect's own env stays clean),
# and copies the resulting manifest back into the scenario directory. The tests
# read those committed manifests, so they need no dbt at run time.
#
# Usage:
#   scripts/refresh_scenarios.sh [case_name ...]   # default: all cases
#
# Assumes ../jaffle_shop_duckdb is checked out next to dblect (it provides
# dbt-duckdb via its own uv environment), matching refresh_jaffle_fixtures.sh.

set -euo pipefail

DBLECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCEN_DIR="${DBLECT_ROOT}/tests/fixtures/scenarios"
BASE="${SCEN_DIR}/base"
CASES_DIR="${SCEN_DIR}/cases"
# The jaffle_shop_duckdb checkout supplies dbt-duckdb via its own uv env. Defaults
# to a sibling of the dblect repo; override with DBLECT_JAFFLE_DIR (handy from a
# git worktree, where the repo root is nested).
JAFFLE="${DBLECT_JAFFLE_DIR:-$(cd "${DBLECT_ROOT}/.." && pwd)/jaffle_shop_duckdb}"

if [[ ! -d "${JAFFLE}" ]]; then
  echo "error: ${JAFFLE} not found (set DBLECT_JAFFLE_DIR to override)." >&2
  echo "       Clone https://github.com/dbt-labs/jaffle_shop_duckdb next to dblect/." >&2
  exit 1
fi

run_dbt() {
  # Compile the project in $1 using jaffle's dbt, writing its manifest in place.
  local project="$1"
  cat >"${project}/profiles.yml" <<YAML
jaffle_shop:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: ${project}/warehouse.duckdb
      threads: 2
YAML
  (
    cd "${JAFFLE}"
    if command -v uv >/dev/null 2>&1 && [[ -f pyproject.toml ]]; then
      uv run dbt compile --project-dir "${project}" --profiles-dir "${project}"
    elif command -v dbt >/dev/null 2>&1; then
      dbt compile --project-dir "${project}" --profiles-dir "${project}"
    else
      echo "error: neither 'uv' nor 'dbt' is available in ${JAFFLE}." >&2
      exit 1
    fi
  )
}

refresh_case() {
  local case_dir="$1"
  local name
  name="$(basename "${case_dir}")"
  echo "==> ${name}"
  local tmp
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/dblect-scenario-${name}.XXXXXX")"
  trap 'rm -rf "${tmp}"' RETURN

  cp -R "${BASE}/." "${tmp}/"
  if [[ -d "${case_dir}/overlay" ]]; then
    cp -R "${case_dir}/overlay/." "${tmp}/"
  fi
  run_dbt "${tmp}"
  cp "${tmp}/target/manifest.json" "${case_dir}/manifest.json"
  echo "    wrote ${case_dir}/manifest.json"
}

if [[ $# -gt 0 ]]; then
  for name in "$@"; do
    refresh_case "${CASES_DIR}/${name}"
  done
else
  for case_dir in "${CASES_DIR}"/*/; do
    refresh_case "${case_dir%/}"
  done
fi
