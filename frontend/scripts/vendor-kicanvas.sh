#!/usr/bin/env bash
# Builds KiCanvas at the pinned commit with our patches and vendors the bundle into
# public/vendor/kicanvas/ (kicanvas.js, LICENSE.md, BUILD_INFO). The result is committed;
# CI re-runs this script and fails if the committed bundle differs (reproducible build).
#
#   ./scripts/vendor-kicanvas.sh            # build + copy
#   KICANVAS_SRC=/path/to/clone ./scripts/vendor-kicanvas.sh   # reuse a clone (faster)
#
# Patches: vendor-patches/kicanvas/*.patch, applied in the order listed in `series`.
set -euo pipefail
FRONTEND="$(cd "$(dirname "$0")/.." && pwd)"
V="$FRONTEND/public/vendor/kicanvas"
PATCHES="$FRONTEND/vendor-patches/kicanvas"
COMMIT="$(tr -d '[:space:]' < "$V/KICANVAS_COMMIT")"
SRC="${KICANVAS_SRC:-$(mktemp -d)/kicanvas}"

if [ ! -d "$SRC/.git" ]; then
  git clone --quiet --filter=blob:none https://github.com/theacodes/kicanvas.git "$SRC"
fi
git -C "$SRC" fetch --quiet origin || true
git -C "$SRC" checkout --quiet --force --detach "$COMMIT"
git -C "$SRC" clean --quiet -fdx -e node_modules

mapfile -t SERIES < <(grep -v -e '^#' -e '^\s*$' "$PATCHES/series")
for p in "${SERIES[@]}"; do
  git -C "$SRC" apply --whitespace=nowarn "$PATCHES/$p"
done

# The host may export NODE_ENV=production, which makes `npm ci` skip devDependencies
# (tsc, esbuild) — spike 1B build note.
(cd "$SRC" && npm ci --include=dev --no-audit --no-fund --loglevel=error && npm run build)

cp "$SRC/build/kicanvas.js" "$V/kicanvas.js"
cp "$SRC/LICENSE.md" "$V/LICENSE.md"
{
  echo "commit=$COMMIT"
  echo "patches=${SERIES[*]}"
  echo "size=$(stat -c %s "$V/kicanvas.js")"
  echo "sha256=$(sha256sum "$V/kicanvas.js" | cut -d' ' -f1)"
} > "$V/BUILD_INFO"
git -C "$SRC" checkout --quiet --force -- .
cat "$V/BUILD_INFO"
