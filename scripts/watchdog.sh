#!/bin/bash
# Polymarket Scanner Watchdog — ensures exactly 1 scanner process
# Called by cron every 5 min. NOT a long-running loop.
#
# Safety layers:
#   1. File lock (fcntl) in scanner.py prevents duplicate instances
#   2. This watchdog uses PID file + ps to verify process health
#   3. Heartbeat file checked for staleness
#
# Exit: 0=healthy/restarted, 1=error

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$SCRIPT_DIR/scanner.pid"
LOCK_FILE="$SCRIPT_DIR/scanner.lock"
HEARTBEAT_FILE="$SCRIPT_DIR/scanner_heartbeat"
LOG_FILE="/tmp/polymarket_scanner.log"
WATCHDOG_LOG="/tmp/polymarket_watchdog.log"
SCANNER_ARGS="--monitor --interval 90 --use-llm"
MAX_HEARTBEAT_AGE=300  # 5 minutes

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] WATCHDOG: $1" >> "$WATCHDOG_LOG"; }

# ── Count scanner processes reliably ──
count_scanners() {
    # Use ps + grep instead of pgrep (more reliable across python path variants)
    ps aux 2>/dev/null | grep -c "[s]canner\.py.*--monitor" || echo 0
}

# ── Kill ALL scanner processes ──
kill_all_scanners() {
    local pids
    pids=$(ps aux 2>/dev/null | grep "[s]canner\.py.*--monitor" | awk '{print $2}' || true)
    if [ -n "$pids" ]; then
        log "Killing PIDs: $(echo $pids | tr '\n' ' ')"
        echo "$pids" | xargs kill 2>/dev/null || true
        sleep 2
        # Force kill survivors
        pids=$(ps aux 2>/dev/null | grep "[s]canner\.py.*--monitor" | awk '{print $2}' || true)
        if [ -n "$pids" ]; then
            log "Force-killing: $(echo $pids | tr '\n' ' ')"
            echo "$pids" | xargs kill -9 2>/dev/null || true
            sleep 1
        fi
    fi
    rm -f "$PID_FILE" "$LOCK_FILE"
}

# ── Start exactly one scanner ──
start_scanner() {
    kill_all_scanners
    sleep 1

    # Verify all dead
    local remaining
    remaining=$(count_scanners | tail -1)
    if [ "$remaining" -gt 0 ]; then
        log "ERROR: $remaining scanners still alive after kill_all. Aborting."
        exit 1
    fi

    cd "$SCRIPT_DIR"
    nohup python3 scanner.py $SCANNER_ARGS >> "$LOG_FILE" 2>&1 &
    local new_pid=$!
    echo "$new_pid" > "$PID_FILE"

    # Verify startup
    sleep 3
    if kill -0 "$new_pid" 2>/dev/null; then
        log "Started PID $new_pid"
    else
        log "ERROR: PID $new_pid died on startup. Check $LOG_FILE"
        rm -f "$PID_FILE"
        exit 1
    fi
}

# ── Check heartbeat freshness ──
heartbeat_stale() {
    [ ! -f "$HEARTBEAT_FILE" ] && return 0
    local age=$(( $(date +%s) - $(date -r "$HEARTBEAT_FILE" +%s 2>/dev/null || echo 0) ))
    [ "$age" -gt "$MAX_HEARTBEAT_AGE" ]
}

# ── PID file valid? ──
pid_alive() {
    [ -f "$PID_FILE" ] || return 1
    local pid
    pid=$(cat "$PID_FILE" 2>/dev/null | tr -d '[:space:]')
    [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

# ═══════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════

PROC_COUNT=$(count_scanners | tail -1)

log "Check: $PROC_COUNT processes"

# Multiple processes → kill all, restart
if [ "$PROC_COUNT" -gt 1 ]; then
    log "ALERT: $PROC_COUNT processes! Killing all."
    start_scanner
    echo "Scanner restarted (killed $PROC_COUNT duplicates, now PID $(cat $PID_FILE))."
    exit 0
fi

# Zero processes → start
if [ "$PROC_COUNT" -eq 0 ]; then
    if heartbeat_stale; then
        log "No scanner running, heartbeat stale. Starting."
    else
        log "No scanner running. Starting."
    fi
    start_scanner
    echo "Scanner restarted (was down, now PID $(cat $PID_FILE))."
    exit 0
fi

# Exactly 1 process — check heartbeat
if heartbeat_stale; then
    local_age="unknown"
    if [ -f "$HEARTBEAT_FILE" ]; then
        local_age="$(( $(date +%s) - $(date -r "$HEARTBEAT_FILE" +%s) ))s"
    fi
    log "Heartbeat stale ($local_age). Restarting."
    start_scanner
    echo "Scanner restarted (heartbeat stale $local_age, now PID $(cat $PID_FILE))."
    exit 0
fi

# Healthy
log "Healthy (1 process, heartbeat fresh)."
echo "Scanner healthy (PID $(cat $PID_FILE 2>/dev/null || echo '?'))."
exit 0
