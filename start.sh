#!/usr/bin/env bash
#
# start.sh — launch the battery monitor with sudo and all necessary setup.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MONITOR="$SCRIPT_DIR/battery_monitor.py"
DATA_DIR="$HOME/.battery-monitor"
PID_FILE="$DATA_DIR/monitor.pid"

usage() {
    cat <<EOF
Usage: ./start.sh [start|stop|status|report|export]

  start   — start the battery monitor in the background (default)
  stop    — stop a running monitor
  status  — show current battery info
  report  — show drain report for the latest session
  export  — export latest session to CSV
  log     — tail the monitor log
EOF
}

ensure_data_dir() {
    mkdir -p "$DATA_DIR"
}

is_running() {
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(<"$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

cmd_start() {
    ensure_data_dir

    if is_running; then
        local pid=$(<"$PID_FILE")
        echo "Monitor is already running (PID $pid)."
        echo "Run './start.sh stop' first, or './start.sh status' to check."
        exit 1
    fi

    # Prompt for sudo upfront so the background process inherits the ticket
    echo "Battery monitor requires sudo for energy-impact data (powermetrics)."
    sudo -v

    # Keep sudo alive in the background while the monitor runs
    (while true; do sudo -n true; sleep 120; done) &
    SUDO_KEEPALIVE_PID=$!

    echo "Starting battery monitor in the background..."
    sudo nohup python3 "$MONITOR" start "$@" \
        >> "$DATA_DIR/monitor.log" 2>&1 &

    # Give it a moment to write its PID file
    sleep 2

    if is_running; then
        local pid=$(<"$PID_FILE")
        echo "Monitor started (PID $pid)."
        echo ""
        echo "  View status  :  ./start.sh status"
        echo "  Tail log     :  ./start.sh log"
        echo "  Stop monitor :  ./start.sh stop"
        echo "  View report  :  ./start.sh report"
        echo ""
        echo "Log: $DATA_DIR/monitor.log"
    else
        echo "Monitor may have failed to start. Check the log:"
        echo "  tail -20 $DATA_DIR/monitor.log"
        kill "$SUDO_KEEPALIVE_PID" 2>/dev/null || true
        exit 1
    fi
}

cmd_stop() {
    if ! is_running; then
        echo "Monitor is not running."
        # Clean up stale PID file if present
        rm -f "$PID_FILE"
        return
    fi

    local pid=$(<"$PID_FILE")
    echo "Stopping monitor (PID $pid)..."
    sudo kill "$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true

    # Wait for it to exit
    for _ in $(seq 1 10); do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "Monitor stopped."
            rm -f "$PID_FILE"
            # Kill any lingering sudo keep-alive processes we spawned
            pkill -f "sudo -n true" 2>/dev/null || true
            return
        fi
        sleep 1
    done

    echo "Monitor didn't stop gracefully — sending SIGKILL."
    sudo kill -9 "$pid" 2>/dev/null || true
    rm -f "$PID_FILE"
}

cmd_status() {
    python3 "$MONITOR" status
}

cmd_report() {
    python3 "$MONITOR" report "$@"
}

cmd_export() {
    python3 "$MONITOR" export "$@"
}

cmd_sessions() {
    python3 "$MONITOR" sessions
}

cmd_log() {
    local logfile="$DATA_DIR/monitor.log"
    if [[ ! -f "$logfile" ]]; then
        echo "No log file yet. Start the monitor first."
        exit 1
    fi
    tail -f "$logfile"
}

# ── Main ──────────────────────────────────────────────────────────

CMD="${1:-start}"
shift || true

case "$CMD" in
    start)    cmd_start "$@" ;;
    stop)     cmd_stop ;;
    status)   cmd_status ;;
    report)   cmd_report "$@" ;;
    export)   cmd_export "$@" ;;
    sessions) cmd_sessions ;;
    log)      cmd_log ;;
    -h|--help|help) usage ;;
    *)
        echo "Unknown command: $CMD"
        usage
        exit 1
        ;;
esac
