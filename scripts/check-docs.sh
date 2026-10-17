#!/usr/bin/env bash
# scripts/check-docs.sh — guards compliance statements in the user-facing docs (spec §12)
# and that docker-compose.yml builds against the pinned KiCad commit. Run from the repo root.
set -euo pipefail
fail() { echo "FAIL: $*"; exit 1; }
for f in README.md docs/deployment/cloudflare.md docs/architecture.md docker-compose.yml .env.example; do
  [ -f "$f" ] || fail "$f missing"
done
grep -q "claude setup-token" README.md || fail "README: setup-token quickstart"
grep -q "Powered by Claude" README.md || fail "README: Powered by Claude"
grep -qi "not Claude Code" README.md || fail "README: not Claude Code statement"
grep -q "Anthropic's terms" README.md || fail "README: Anthropic terms statement"
grep -q "GPL-3.0-only" README.md || fail "README: license"
grep -q "docker compose up" README.md || fail "README: compose quickstart"
grep -q "no file, shell or web tools" README.md || fail "README: agent tool security note"
if grep -qE '!\[[^]]*\]\(' README.md; then fail "README: no screenshots in the foundation"; fi
grep -q "16.4" docs/deployment/cloudflare.md || fail "cloudflare.md: iOS 16.4"
grep -q "Cloudflare Access" docs/deployment/cloudflare.md || fail "cloudflare.md: Access"
grep -q "claude setup-token" .env.example || fail ".env.example: setup-token hint"
commit="$(tr -d '[:space:]' < docker/kicad-base/KICAD_COMMIT)"
grep -q "KICAD_COMMIT: ${commit}\$" docker-compose.yml \
  || fail "docker-compose.yml: build arg KICAD_COMMIT must be ${commit} (docker/kicad-base/KICAD_COMMIT)"
echo OK
