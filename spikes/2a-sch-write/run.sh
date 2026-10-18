#!/usr/bin/env bash
# Runs a command inside the pinned kicad-base image with this directory mounted at /spike.
# Usage: ./run.sh python run_all.py | ./run.sh ./make_fixtures.sh | ./run.sh python q03_connect.py
set -euo pipefail
cd "$(dirname "$0")"
IMG="${KICAD_IMAGE:-ghcr.io/peterus/copperchat-kicad-base:0616634034fa1a8c92c141c293342cbef2f68dff-r41da5e29cdd40d32}"
cmd=("$@")
[ "${cmd[0]:-}" = python ] && cmd[0]=/opt/venv/bin/python
exec docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/spike" -w /spike \
  -e PYTHONPATH=/spike/.deps -e PYTHONUNBUFFERED=1 -e HOME=/tmp "$IMG" "${cmd[@]}"
