#!/usr/bin/env bash
# Convenience launcher: ./run.sh deployments/<server>
set -euo pipefail
ENV_DIR="${1:-deployments/test}"
if [ ! -d "$ENV_DIR" ]; then
  echo "deployment directory not found: $ENV_DIR" >&2
  echo "usage: ./run.sh deployments/<server>" >&2
  exit 1
fi
exec python3 main.py --env-dir "$ENV_DIR"
