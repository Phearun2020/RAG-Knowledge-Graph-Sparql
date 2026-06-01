#!/usr/bin/env bash
set -euo pipefail

FUSEKI_DATASET="${FUSEKI_DATASET:-kg}"
FUSEKI_URL="${FUSEKI_URL:-http://localhost:3031}"
FUSEKI_USER="${FUSEKI_USER:-admin}"
FUSEKI_PASSWORD="${FUSEKI_PASSWORD:-localpass}"
TTL_FILE="${1:-data/sample-kg.ttl}"

curl --fail --silent --show-error \
  --user "${FUSEKI_USER}:${FUSEKI_PASSWORD}" \
  --header "Content-Type: text/turtle" \
  --request POST \
  --data-binary @"${TTL_FILE}" \
  "${FUSEKI_URL}/${FUSEKI_DATASET}/data"

echo "Loaded ${TTL_FILE} into ${FUSEKI_URL}/${FUSEKI_DATASET}"
