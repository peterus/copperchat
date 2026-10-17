#!/usr/bin/env bash
# scripts/e2e-token-missing.sh IMAGE — the container must refuse to start without model
# credentials and point the user to `claude setup-token`.
set -euo pipefail
image="${1:?usage: e2e-token-missing.sh IMAGE}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/projects" "$tmp/data" && chmod 777 "$tmp/data"
set +e
out="$(timeout 120 docker run --rm -e ADMIN_PASSWORD=x -v "$tmp/projects:/projects:ro" -v "$tmp/data:/data" "$image" 2>&1)"
code=$?
set -e
echo "$out"
if [ "$code" -eq 0 ]; then echo "FAIL: container exited 0 without a token"; exit 1; fi
if [ "$code" -eq 124 ]; then echo "FAIL: container kept running without a token"; exit 1; fi
grep -q "claude setup-token" <<<"$out" || { echo "FAIL: message does not mention 'claude setup-token'"; exit 1; }
echo "OK: exit $code with setup-token hint"
