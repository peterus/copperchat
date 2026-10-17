#!/usr/bin/env bash
# Clone KiCanvas at the pinned commit, build it, copy bundle + license to vendor/.
# Supplementary experiment only: KICANVAS_PATCH=<patch file> VENDOR_DIR=vendor-patched
# applies a patch before building (the gate result uses the unpatched build).
# KICANVAS_PATCH may list several space-separated patch files, applied in order.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
COMMIT="$(tr -d '[:space:]' < "$HERE/KICANVAS_COMMIT")"
SRC="$HERE/.cache/kicanvas"
if [ ! -d "$SRC/.git" ]; then
  git clone --filter=blob:none https://github.com/theacodes/kicanvas.git "$SRC"
fi
git -C "$SRC" fetch --quiet origin
git -C "$SRC" checkout --quiet --force --detach "$COMMIT"
VENDOR="$HERE/${VENDOR_DIR:-vendor}"
for p in ${KICANVAS_PATCH:-}; do git -C "$SRC" apply "$(realpath "$p")"; done
(cd "$SRC" && npm ci --include=dev --no-audit --no-fund && npm run build)
if [ -n "${KICANVAS_PATCH:-}" ]; then git -C "$SRC" checkout --quiet -- .; fi
mkdir -p "$VENDOR"
cp "$SRC/build/kicanvas.js" "$VENDOR/kicanvas.js"
cp "$SRC/LICENSE.md" "$VENDOR/LICENSE.md"
{
  echo "commit=$COMMIT"
  echo "patch=${KICANVAS_PATCH:-none}"
  echo "commit_date=$(git -C "$SRC" show -s --format=%cI HEAD)"
  echo "node=$(node --version) npm=$(npm --version)"
  echo "size=$(stat -c %s "$VENDOR/kicanvas.js")"
  echo "sha256=$(sha256sum "$VENDOR/kicanvas.js" | cut -d' ' -f1)"
} > "$VENDOR/BUILD_INFO"
cat "$VENDOR/BUILD_INFO"
