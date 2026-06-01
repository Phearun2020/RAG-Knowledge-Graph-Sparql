#!/usr/bin/env bash
set -euo pipefail

FUSEKI_DATASET="${FUSEKI_DATASET:-kg}"
FUSEKI_URL="${FUSEKI_URL:-http://localhost:3031}"
QUERY_FILE="${1:-scripts/sample-query.rq}"

curl --fail --silent --show-error \
  --header "Accept: application/sparql-results+json" \
  --data-urlencode "query@${QUERY_FILE}" \
  "${FUSEKI_URL}/${FUSEKI_DATASET}/sparql"
