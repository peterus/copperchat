#!/usr/bin/env bash
# scripts/live-upgrade-check.sh — opt-in upgrade check v0.2.0 -> this checkout with the real
# model (sessions plan C6 Task 1). Costs two short model turns.
#   1. ghcr.io/peterus/copperchat:0.2.0 starts on a fresh data dir with the blank fixture
#      project; the agent is told a code word (backend/tests/live/upgrade_seed_v020.py).
#   2. backend/tests/live/test_upgrade_live.py runs in the kicad-base image with the same
#      /projects and /data paths: the app migrates the database, opens "Session 1" and asks
#      for the code word (the resumed SDK session must remember it).
# Needs docker, uv and CLAUDE_CODE_OAUTH_TOKEN (or ANTHROPIC_API_KEY) in the environment.
# Run from the repo root. The result goes to backend/tests/live/results/<date>.json.
set -euo pipefail
: "${CLAUDE_CODE_OAUTH_TOKEN:=}" "${ANTHROPIC_API_KEY:=}"
[ -n "${CLAUDE_CODE_OAUTH_TOKEN}${ANTHROPIC_API_KEY}" ] || { echo "no model credentials"; exit 1; }
# The v0.2.0 release image, pinned by digest (it receives real model credentials).
old_image="ghcr.io/peterus/copperchat:0.2.0@sha256:ef12a429f5832ebbcf8529fcb4a5d73385f5d8bce173c3f1c3ba2721ac7bc4a7"
base_image="$(scripts/kicad-base-image.sh tag "$(tr -d '[:space:]' < docker/kicad-base/KICAD_COMMIT)")"
work="$(mktemp -d)"
name="copperchat-upgrade-$$"
password="upgrade-check-$$"
word="PELICAN-$RANDOM"
port="${UPGRADE_CHECK_PORT:-18093}"
cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; rm -rf "$work"; }
trap cleanup EXIT

mkdir -p "$work/projects" "$work/data" "$work/home"
cp -r backend/tests/fixtures/projects/blank "$work/projects/blank"
docker run -d --name "$name" --user "$(id -u):$(id -g)" -p "127.0.0.1:${port}:8080" \
  -e CLAUDE_CODE_OAUTH_TOKEN -e ANTHROPIC_API_KEY -e ADMIN_PASSWORD="$password" \
  -e COOKIE_SECURE=false -v "$work/projects:/projects" -v "$work/data:/data" \
  "$old_image" >/dev/null
healthy=""
for _ in $(seq 60); do
  curl -fsS "http://127.0.0.1:${port}/api/health" >/dev/null 2>&1 && { healthy=1; break; }
  sleep 2
done
[ -n "$healthy" ] || { echo "v0.2.0 container did not become healthy"; docker logs "$name" | tail -40; exit 1; }
(cd backend && uv run python -m tests.live.upgrade_seed_v020 "http://127.0.0.1:${port}" \
  "$password" "$word")
docker stop "$name" >/dev/null

repo="$(pwd)"
uv_bin="$(command -v uv)"
uv_cache="${UV_CACHE_HOST:-$HOME/.cache/uv}"
mkdir -p "$uv_cache"  # a missing bind-mount source would be created root-owned
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp/home \
  -e UV_PROJECT_ENVIRONMENT=/tmp/home/venv -e UV_CACHE_DIR=/tmp/home/uv-cache \
  -e CLAUDE_CODE_OAUTH_TOKEN -e ANTHROPIC_API_KEY \
  -e COPPERCHAT_UPGRADE_DATA=/data -e COPPERCHAT_UPGRADE_PROJECTS=/projects \
  -e COPPERCHAT_UPGRADE_WORD="$word" \
  -v "$work/projects:/projects" -v "$work/data:/data" -v "$work/home:/tmp/home" \
  -v "$uv_cache:/tmp/home/uv-cache" \
  -v "$repo:$repo" -w "$repo/backend" -v "$uv_bin:/usr/local/bin/uv:ro" "$base_image" \
  bash -c 'uv python install 3.13 >/dev/null && uv sync --locked -q &&
    uv run pytest -m live_sdk tests/live/test_upgrade_live.py -q -s -p no:cacheprovider'
