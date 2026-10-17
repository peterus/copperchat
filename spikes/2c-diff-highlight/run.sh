#!/usr/bin/env bash
# usage: ./run.sh harness/script.py [args...]  — runs inside the pinned kicad-base image
set -euo pipefail
cd "$(dirname "$0")/../.."
IMG="${IMG:-ghcr.io/peterus/copperchat-kicad-base:0616634034fa1a8c92c141c293342cbef2f68dff-r41da5e29cdd40d32}"
SCRIPT="$1"; shift
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -e PAIRS=/w/spikes/2c-diff-highlight/.work/pairs -v "$PWD:/w" -w /w/spikes/2c-diff-highlight/harness \
  --entrypoint /opt/venv/bin/python "$IMG" "/w/spikes/2c-diff-highlight/$SCRIPT" "$@"
