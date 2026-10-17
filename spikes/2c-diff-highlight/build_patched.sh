#!/usr/bin/env bash
# Builds the baseline bundle (= the vendored one) and the spike-patched KiCanvas into .work/bundles/.
set -euo pipefail
cd "$(dirname "$0")"
COMMIT="$(tr -d '[:space:]' < ../../frontend/public/vendor/kicanvas/KICANVAS_COMMIT)"
mkdir -p .work/bundles
if [ ! -d .work/kicanvas/.git ]; then
  git clone --quiet --filter=blob:none https://github.com/peterus/kicanvas.git .work/kicanvas
fi
git -C .work/kicanvas fetch --quiet origin "$COMMIT" || true
git -C .work/kicanvas checkout --quiet --force --detach "$COMMIT"
git -C .work/kicanvas clean --quiet -fdx -e node_modules
git -C .work/kicanvas apply "$PWD/patches/schematic-highlight-set.patch"
# NODE_ENV=production would skip devDependencies (tsc, esbuild)
(cd .work/kicanvas && npm ci --include=dev --no-audit --no-fund --loglevel=error && npm run build >/dev/null)
cp .work/kicanvas/build/kicanvas.js .work/bundles/patched.js
cp ../../frontend/public/vendor/kicanvas/kicanvas.js .work/bundles/baseline.js
git -C .work/kicanvas checkout --quiet --force -- .
sha256sum .work/bundles/*.js
