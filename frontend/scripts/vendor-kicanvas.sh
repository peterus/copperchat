#!/usr/bin/env bash
# Builds KiCanvas at the pinned commit of our fork and vendors the bundle into
# public/vendor/kicanvas/ (kicanvas.js, LICENSE.md, BUILD_INFO). The result is committed;
# CI re-runs this script and fails if the committed bundle differs (reproducible build).
#
#   ./scripts/vendor-kicanvas.sh            # build + copy
#   KICANVAS_SRC=/path/to/clone ./scripts/vendor-kicanvas.sh   # reuse a clone (faster)
#
# The pin (KICANVAS_COMMIT) is a full SHA on branch `copperchat` of KICANVAS_REPO: upstream
# KiCanvas plus CopperChat's changes as commits (see vendor-patches/kicanvas/README.md).
set -euo pipefail
FRONTEND="$(cd "$(dirname "$0")/.." && pwd)"
V="$FRONTEND/public/vendor/kicanvas"
KICANVAS_REPO="https://github.com/peterus/kicanvas.git"
COMMIT="$(tr -d '[:space:]' < "$V/KICANVAS_COMMIT")"
SRC="${KICANVAS_SRC:-$(mktemp -d)/kicanvas}"

if [ ! -d "$SRC/.git" ]; then
  git clone --quiet --filter=blob:none "$KICANVAS_REPO" "$SRC"
fi
git -C "$SRC" fetch --quiet "$KICANVAS_REPO" "$COMMIT" || true
git -C "$SRC" checkout --quiet --force --detach "$COMMIT"
git -C "$SRC" clean --quiet -fdx -e node_modules

# The host may export NODE_ENV=production, which makes `npm ci` skip devDependencies
# (tsc, esbuild) — spike 1B build note.
(cd "$SRC" && npm ci --include=dev --no-audit --no-fund --loglevel=error && npm run build)

cp "$SRC/build/kicanvas.js" "$V/kicanvas.js"
cp "$SRC/LICENSE.md" "$V/LICENSE.md"
{
  echo "repo=$KICANVAS_REPO"
  echo "commit=$COMMIT"
  echo "size=$(stat -c %s "$V/kicanvas.js")"
  echo "sha256=$(sha256sum "$V/kicanvas.js" | cut -d' ' -f1)"
} > "$V/BUILD_INFO"
git -C "$SRC" checkout --quiet --force -- .
cat "$V/BUILD_INFO"
