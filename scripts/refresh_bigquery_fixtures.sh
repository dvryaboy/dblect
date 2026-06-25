#!/usr/bin/env bash
# Regenerate the vendored BigQuery jaffle manifest fixture.
#
# Compiles the self-contained tests/fixtures/jaffle_project against BigQuery so
# the committed manifest carries real BigQuery-dialect compiled SQL (backtick
# identifiers, the bigquery sqlglot dialect). The GCP project/dataset names in the
# compiled SQL are rewritten to neutral placeholders, so no real project name is
# committed and the detectors (which read SQL structure, not real relations) are
# unaffected.
#
# Maintainer-only: needs dbt-bigquery and BigQuery OAuth/ADC. End users and CI
# read the committed manifest and never run this.
#
# Usage:
#   DBLECT_BQ_PROJECT=<a-bq-project-you-can-reach> scripts/refresh_bigquery_fixtures.sh
#
# dbt-bigquery is not a dblect dependency; point DBT at an environment that has it:
#   DBT=/path/to/venv/bin/dbt DBLECT_BQ_PROJECT=my-proj scripts/refresh_bigquery_fixtures.sh
set -euo pipefail

DBLECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_DIR="${DBLECT_ROOT}/tests/fixtures/jaffle_project"
FIXTURE_DIR="${DBLECT_ROOT}/tests/fixtures/jaffle_bigquery"
DBT="${DBT:-dbt}"
BQ_PROJECT="${DBLECT_BQ_PROJECT:?set DBLECT_BQ_PROJECT to a BigQuery project you can reach}"

PROFILES_DIR="$(mktemp -d)"
trap 'rm -rf "${PROFILES_DIR}"' EXIT
cat > "${PROFILES_DIR}/profiles.yml" <<YAML
jaffle_shop:
  target: bq
  outputs:
    bq:
      type: bigquery
      method: oauth
      project: ${BQ_PROJECT}
      dataset: dblect_fixture_jaffle
      threads: 4
      location: US
      job_execution_timeout_seconds: 120
      job_retries: 0
YAML

echo "Compiling jaffle_project against BigQuery (${BQ_PROJECT})..."
( cd "${PROJECT_DIR}" && DBT_PROFILES_DIR="${PROFILES_DIR}" "${DBT}" compile --no-partial-parse )

mkdir -p "${FIXTURE_DIR}"
# Rewrite the real project/dataset to neutral placeholders so nothing
# environment-specific is committed.
python3 - "${PROJECT_DIR}/target/manifest.json" "${FIXTURE_DIR}/manifest.json" "${BQ_PROJECT}" <<'PY'
import json, sys
src, dst, real_project = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(src).read()
text = text.replace(real_project, "dblect-demo").replace("dblect_fixture_jaffle", "jaffle")
json.loads(text)  # ensure still valid JSON after the rewrite
open(dst, "w").write(text)
PY
echo "Wrote ${FIXTURE_DIR}/manifest.json ($(wc -c < "${FIXTURE_DIR}/manifest.json") bytes)"
