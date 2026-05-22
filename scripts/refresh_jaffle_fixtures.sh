#!/usr/bin/env bash
# Regenerate the vendored jaffle_shop_duckdb manifest fixture.
#
# Run from anywhere; assumes ../jaffle_shop_duckdb is checked out next to dblect.
#
# Usage:
#   scripts/refresh_jaffle_fixtures.sh

set -euo pipefail

DBLECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JAFFLE="$(cd "${DBLECT_ROOT}/.." && pwd)/jaffle_shop_duckdb"
FIXTURE_DIR="${DBLECT_ROOT}/tests/fixtures/jaffle"

if [[ ! -d "${JAFFLE}" ]]; then
  echo "error: ${JAFFLE} not found." >&2
  echo "       Clone https://github.com/dbt-labs/jaffle_shop_duckdb next to dblect/." >&2
  exit 1
fi

# Invoke dbt via jaffle's own dependency environment so we don't pollute dblect's.
# Prefer `uv run` (matches jaffle's pyproject.toml setup); fall back to a system dbt.
# `dbt compile` (rather than `dbt parse`) is what populates `compiled_code` on
# every model, which is what dblect's analysis layer consumes.
echo "Running 'dbt compile' in ${JAFFLE}..."
(
  cd "${JAFFLE}"
  if command -v uv >/dev/null 2>&1 && [[ -f pyproject.toml ]]; then
    uv run dbt compile
  elif command -v dbt >/dev/null 2>&1; then
    dbt compile
  else
    echo "error: neither 'uv' nor 'dbt' is available." >&2
    echo "       Install uv (https://docs.astral.sh/uv) or dbt-duckdb." >&2
    exit 1
  fi
)

mkdir -p "${FIXTURE_DIR}"
cp "${JAFFLE}/target/manifest.json" "${FIXTURE_DIR}/manifest.json"

echo "Wrote ${FIXTURE_DIR}/manifest.json ($(wc -c < "${FIXTURE_DIR}/manifest.json") bytes)"

# Derived fixture for the unvalidated-adapter gate. Same SQL, adapter_type
# relabeled to snowflake. The gate checks the metadata field, so a relabeled
# copy exercises the bail path against a real manifest without standing up a
# Snowflake-built jaffle alongside the duckdb one.
SNOWFLAKE_FIXTURE_DIR="${DBLECT_ROOT}/tests/fixtures/jaffle_snowflake_meta"
mkdir -p "${SNOWFLAKE_FIXTURE_DIR}"
python3 - <<PY
import json, pathlib
src = pathlib.Path("${FIXTURE_DIR}/manifest.json")
dst = pathlib.Path("${SNOWFLAKE_FIXTURE_DIR}/manifest.json")
m = json.loads(src.read_text())
m["metadata"]["adapter_type"] = "snowflake"
dst.write_text(json.dumps(m))
PY
echo "Wrote ${SNOWFLAKE_FIXTURE_DIR}/manifest.json ($(wc -c < "${SNOWFLAKE_FIXTURE_DIR}/manifest.json") bytes)"
