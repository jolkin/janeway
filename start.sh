#!/bin/bash
# Container entrypoint.
# Launches the uvicorn wrapper (server.py) which in turn manages all child
# services (kirk-serve, pykirk dispatcher/agent/oracle) via Python subprocess.
set -euo pipefail

echo "========================================"
echo "Execution-as-a-Service"
echo "========================================"
echo "Kirk binary : ${KIRK_BINARY:-/app/kirk/kirk}"
echo "PyKirk dir  : ${PYKIRK_DIR:-/app/pykirk}"
echo "Kirk port   : ${KIRK_PORT:-7000}  (internal)"
echo "Dispatcher  : ${DISPATCHER_PORT:-9000}  (internal)"
echo "Agent       : ${LOCAL_AGENT_PORT:-9001}  (internal)"
echo "Oracle      : ${LOCAL_ORACLE_PORT:-9002}  (internal)"
echo "Server port : ${SERVER_PORT:-8000}  (exposed)"
echo "========================================"

exec uvicorn server:app \
    --host 0.0.0.0 \
    --port "${SERVER_PORT:-8000}" \
    --log-level info
