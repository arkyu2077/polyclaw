#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Activate venv
if [ -d ".venv" ]; then
    source .venv/bin/activate
fi

# Start scanner
echo "🚀 Starting Polyclaw scanner..."
nohup python3 src/scanner.py --monitor --interval 90 --use-llm >> data/scanner.log 2>&1 &
PID=$!
echo $PID > data/scanner.pid
echo "✅ Scanner started (PID: $PID)"
echo "📋 Logs: tail -f data/scanner.log"
