#!/bin/bash
set -e

echo "=== Starting Trading Agent (background) ==="
cd /home/runner/workspace/trading-agent && python main.py &
AGENT_PID=$!
echo "Trading agent started (PID: $AGENT_PID)"

echo "=== Starting API Server (foreground) ==="
exec node --enable-source-maps /home/runner/workspace/artifacts/api-server/dist/index.mjs
