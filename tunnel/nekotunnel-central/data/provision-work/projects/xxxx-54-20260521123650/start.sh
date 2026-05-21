#!/bin/sh
set -eu

FRPS_PID=""
SSLH_PID=""
STOPPING=0

log() {
    echo "[nekotunnel] $*"
}

stop_all() {
    STOPPING=1
    log "Stopping services..."

    if [ -n "$SSLH_PID" ]; then
        kill -TERM "$SSLH_PID" 2>/dev/null || true
    fi

    if [ -n "$FRPS_PID" ]; then
        kill -TERM "$FRPS_PID" 2>/dev/null || true
    fi

    wait "$SSLH_PID" 2>/dev/null || true
    wait "$FRPS_PID" 2>/dev/null || true

    log "Stopped."
}

trap 'stop_all; exit 0' INT TERM

run_frps() {
    while [ "$STOPPING" -eq 0 ]; do
        log "Starting frps..."
        /frp/frps -c /frp/frps.toml &
        FRPS_PID=$!
        wait "$FRPS_PID" || true

        if [ "$STOPPING" -eq 0 ]; then
            log "frps exited unexpectedly. Restarting in 2 seconds..."
            sleep 2
        fi
    done
}

run_sslh() {
    while [ "$STOPPING" -eq 0 ]; do
        if sslh -h 2>&1 | grep -q -- '--tls'; then
            TLS_OPT="--tls"
        else
            TLS_OPT="--ssl"
        fi

        log "Starting sslh on port ${PORT:-8080}..."

        sslh -f -u root           -p "0.0.0.0:${PORT:-8080}"           "$TLS_OPT" "127.0.0.1:7000"           --anyprot "127.0.0.1:6000"           --timeout 2 &
        SSLH_PID=$!

        wait "$SSLH_PID" || true

        if [ "$STOPPING" -eq 0 ]; then
            log "sslh exited unexpectedly. Restarting in 2 seconds..."
            sleep 2
        fi
    done
}

run_frps &
FRPS_SUPERVISOR_PID=$!

run_sslh &
SSLH_SUPERVISOR_PID=$!

wait "$FRPS_SUPERVISOR_PID" "$SSLH_SUPERVISOR_PID"

