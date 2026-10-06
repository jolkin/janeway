#!/usr/bin/env bash
# Submit the multi-drone drone-firefighting PDDL example (drone1 + drone2,
# house1/2/3) to a running Janeway server.
#
# Assumes Janeway is reachable at $EAAS_URL (default http://localhost:8000)
# and that drone_plan_multi.txt has been supplied alongside the other
# example files.
#
# Usage:
#   ./run_multi.sh                            # uses http://localhost:8000
#   EAAS_URL=http://other:8000 ./run_multi.sh

set -euo pipefail

EAAS_URL="${EAAS_URL:-http://localhost:8000}"
HERE="$(cd "$(dirname "$0")" && pwd)"

curl --fail-with-body -X POST "${EAAS_URL}/execute-pddl" \
     -F "domain=@${HERE}/drone_domain.pddl" \
     -F "problem=@${HERE}/drone_problem_multi.pddl" \
     -F "plan_file=@${HERE}/drone_plan_multi.txt"
echo
