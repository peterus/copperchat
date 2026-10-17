#!/usr/bin/env bash
# Usage: ./run.sh q01_load.py   (runs inside the pinned kicad-base image; results/ is written here)
set -euo pipefail
cd "$(dirname "$0")"
IMAGE="${KICAD_BASE_IMAGE:-ghcr.io/peterus/copperchat-kicad-base:0616634034fa1a8c92c141c293342cbef2f68dff}"
mkdir -p results
exec docker run --rm --user "$(id -u):$(id -g)" --entrypoint /opt/venv/bin/python \
  -e PYTHONPATH=/spike -e SPIKE_RESULTS=/spike/results -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$PWD:/spike" "$IMAGE" "/spike/$@"
