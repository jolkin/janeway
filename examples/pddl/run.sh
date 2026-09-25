#!/usr/bin/env bash
# Submit the drone PDDL example to a running Janeway server.
#
# Assumes Janeway is reachable at $EAAS_URL (default http://localhost:8000)
# and the three example files live in this directory.
#
# Usage:
#   ./run.sh                            # uses http://localhost:8000
#   EAAS_URL=http://other:8000 ./run.sh

set -euo pipefail

EAAS_URL="${EAAS_URL:-http://localhost:8000}"
HERE="$(cd "$(dirname "$0")" && pwd)"

curl --fail-with-body -X POST "${EAAS_URL}/execute-pddl" \
     -F "domain=@${HERE}/drone_domain.pddl" \
     -F "problem=@${HERE}/drone_problem.pddl" \
     -F "plan_file=@${HERE}/drone_plan.txt"
echo
