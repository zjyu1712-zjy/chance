#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PORT="${PORT:-${DEPLOY_RUN_PORT:-5000}}"
exec node server.mjs
