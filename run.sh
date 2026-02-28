#!/bin/bash
# Polyclaw — manual start for development/debugging.
# In production, use OpenClaw to manage this process.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ -d ".venv" ]; then
    source .venv/bin/activate
fi

echo "Starting Polyclaw scanner..."
echo "Press Ctrl+C to stop, or send SIGTERM for graceful shutdown."
exec python3 src/scanner.py --monitor --interval 90 --use-llm "$@"
