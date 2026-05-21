#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ -f .run/pinggy.pid ]; then
  kill "$(cat .run/pinggy.pid)" 2>/dev/null || true
  rm -f .run/pinggy.pid
fi

if [ -f .run/uvicorn.pid ]; then
  kill "$(cat .run/uvicorn.pid)" 2>/dev/null || true
  rm -f .run/uvicorn.pid
fi

pkill -f 'ssh .*free\.pinggy\.io' 2>/dev/null || true

echo "Stopped uvicorn and Pinggy tunnel."
