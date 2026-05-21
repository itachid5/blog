#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p logs .run

if [ ! -d .venv ]; then
  python -m venv .venv
fi

.venv/bin/python -m pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
fi

health_ok=0
if [ -f .run/uvicorn.pid ] && kill -0 "$(cat .run/uvicorn.pid)" 2>/dev/null; then
  if .venv/bin/python - <<'PY' >/dev/null 2>&1
import urllib.request
urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)
PY
  then
    health_ok=1
  fi
fi

if [ "$health_ok" -ne 1 ]; then
  if [ -f .run/uvicorn.pid ]; then
    kill "$(cat .run/uvicorn.pid)" 2>/dev/null || true
  fi
  .venv/bin/python -m uvicorn --env-file .env app.main:app --host 127.0.0.1 --port 8080 --reload > logs/uvicorn.log 2>&1 &
  echo $! > .run/uvicorn.pid
fi

for i in {1..40}; do
  if .venv/bin/python - <<'PY' >/dev/null 2>&1
import urllib.request
urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)
PY
  then
    break
  fi
  if [ "$i" -eq 40 ]; then
    echo "FastAPI did not become healthy. See logs/uvicorn.log" >&2
    exit 1
  fi
  sleep 1
done

if [ -f .run/pinggy.pid ]; then
  kill "$(cat .run/pinggy.pid)" 2>/dev/null || true
fi

: > logs/pinggy.log
nohup ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30 -p 443 -R0:localhost:8080 free.pinggy.io > logs/pinggy.log 2>&1 &
echo $! > .run/pinggy.pid

pinggy_url=""
for i in {1..60}; do
  pinggy_url=$(.venv/bin/python - <<'PY'
import re
from pathlib import Path
text = Path('logs/pinggy.log').read_text(errors='ignore') if Path('logs/pinggy.log').exists() else ''
text = re.sub(r'\x1b\[[0-9;?]*[ -/]*[@-~]', '', text)
match = re.search(r'https://[a-zA-Z0-9.-]+\.pinggy(?:-free)?\.link', text)
print(match.group(0) if match else '')
PY
)
  if [ -n "$pinggy_url" ]; then
    break
  fi
  if ! kill -0 "$(cat .run/pinggy.pid)" 2>/dev/null; then
    echo "Pinggy tunnel exited early. See logs/pinggy.log" >&2
    exit 1
  fi
  sleep 1
done

if [ -z "$pinggy_url" ]; then
  echo "Could not find Pinggy URL. See logs/pinggy.log" >&2
  exit 1
fi

admin_token=$(awk -F= '/^ADMIN_TOKEN=/{print substr($0, index($0,$2)); exit}' .env)

printf 'Local URL: http://127.0.0.1:8080\n'
printf 'Pinggy public URL: %s\n' "$pinggy_url"
printf 'Admin token: %s\n' "$admin_token"
printf 'Uvicorn PID: %s\n' "$(cat .run/uvicorn.pid)"
printf 'Pinggy PID: %s\n' "$(cat .run/pinggy.pid)"
printf 'Stop with: ./stop_public_pinggy.sh\n'
