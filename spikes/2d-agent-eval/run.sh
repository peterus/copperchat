#!/usr/bin/env bash
# Runs a command inside the pinned kicad-base image with the repository mounted at /repo
# (this spike reuses spikes/2a-sch-write, spikes/2b-libraries and the built-in style skill).
# Model credentials: CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY from the environment, else
# read from $COPPERCHAT_ENV (default ~/copperchat/.env) — passed by name, never on the command line.
# Usage: ./run.sh python scripted.py | ./run.sh python eval.py --scenario s1 --run 1
set -euo pipefail
cd "$(dirname "$0")"
IMG="${KICAD_IMAGE:-ghcr.io/peterus/copperchat-kicad-base:0616634034fa1a8c92c141c293342cbef2f68dff}"
ENV_FILE="${COPPERCHAT_ENV:-$HOME/copperchat/.env}"
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}${ANTHROPIC_API_KEY:-}" ] && [ -f "$ENV_FILE" ]; then
  CLAUDE_CODE_OAUTH_TOKEN="$(sed -n 's/^CLAUDE_CODE_OAUTH_TOKEN=//p' "$ENV_FILE" | tail -n1)"
  ANTHROPIC_API_KEY="$(sed -n 's/^ANTHROPIC_API_KEY=//p' "$ENV_FILE" | tail -n1)"
  export CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY
fi
cmd=("$@")
[ "${cmd[0]:-}" = python ] && cmd[0]=/opt/venv/bin/python
exec docker run --rm --user "$(id -u):$(id -g)" -v "$PWD/../..:/repo" -w /repo/spikes/2d-agent-eval \
  -e PYTHONPATH=/repo/spikes/2d-agent-eval/.deps:/repo/spikes/2d-agent-eval:/repo/spikes/2a-sch-write:/repo/spikes/2b-libraries \
  -e PYTHONUNBUFFERED=1 -e PYTHONDONTWRITEBYTECODE=1 -e HOME=/tmp \
  -e CLAUDE_CODE_OAUTH_TOKEN -e ANTHROPIC_API_KEY "$IMG" "${cmd[@]}"
