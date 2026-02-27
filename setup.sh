#!/bin/bash
set -e
echo "🚀 Polyclaw — Setup"
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
    echo "❌ Python 3 required. Install it first."
    exit 1
fi

# Create venv
if [ ! -d ".venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv .venv
fi
source .venv/bin/activate

# Install deps
echo "📦 Installing dependencies..."
pip install -q -r requirements.txt

# Bootstrap .env if missing
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo ""
    echo "📝 Created .env — edit it with your private key:"
    echo "   nano .env"
    echo ""
    echo "Then run: ./setup.sh again to derive CLOB credentials."
    exit 0
fi

# Load .env to check if private key is set
set -a
source .env
set +a

if [ -z "$POLYMARKET_PRIVATE_KEY" ] || [ "$POLYMARKET_PRIVATE_KEY" = "0x..." ]; then
    echo "⚠️  Set POLYMARKET_PRIVATE_KEY in .env first"
    exit 1
fi

# Derive CLOB creds from private key and write back to .env
python3 - <<'PYEOF'
import os
from dotenv import load_dotenv, set_key
from pathlib import Path

load_dotenv()
pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")

if not pk or pk == "0x...":
    print("⚠️  POLYMARKET_PRIVATE_KEY not set in .env")
    exit(1)

from py_clob_client.client import ClobClient
client = ClobClient("https://clob.polymarket.com", key=pk, chain_id=137, signature_type=0)
creds = client.derive_api_key()
address = client.get_address()

env_file = Path(".env")
set_key(str(env_file), "POLYMARKET_CLOB_API_KEY", creds.api_key)
set_key(str(env_file), "POLYMARKET_CLOB_API_SECRET", creds.api_secret)
set_key(str(env_file), "POLYMARKET_CLOB_API_PASSPHRASE", creds.api_passphrase)
set_key(str(env_file), "POLYMARKET_WALLET_ADDRESS", address)

print(f"✅ CLOB credentials derived for {address}")
PYEOF

echo ""
echo "✅ Setup complete! Run: ./run.sh"
