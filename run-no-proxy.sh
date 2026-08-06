#!/usr/bin/env bash
# ============================================================================
# run-no-proxy.sh - launch the anime-world-stremio-addon in no-proxy mode
#
# What this does:
#   1. Creates a python venv if missing
#   2. Installs deps from requirements.txt
#   3. Copies .env.example -> .env if .env doesn't exist
#   4. Starts the Flask dev server with STREAM_MODE=direct (no video proxy)
#
# Once running, open http://localhost:5000/ in Stremio to install the addon.
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 1. venv
if [ ! -d .venv ]; then
  echo ">> creating python venv"
  python3 -m venv .venv
fi

# 2. deps
echo ">> installing deps (cached after first run)"
.venv/bin/pip install -q -r requirements.txt

# 3. .env
if [ ! -f .env ]; then
  echo ">> seeding .env from .env.example (edit TMDB_API_KEY before going live!)"
  cp .env.example .env
fi

# 4. default to direct mode if not set
export STREAM_MODE="${STREAM_MODE:-direct}"
export ENABLE_PROXY_ROUTES="${ENABLE_PROXY_ROUTES:-0}"

echo ">> launching addon with STREAM_MODE=$STREAM_MODE  ENABLE_PROXY_ROUTES=$ENABLE_PROXY_ROUTES"
echo ">> open http://localhost:5000/ in Stremio to install"
exec .venv/bin/python run.py
