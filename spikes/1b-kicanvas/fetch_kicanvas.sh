#!/usr/bin/env bash
# Clone KiCanvas at the pinned commit, build it, copy bundle + license to vendor/.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
COMMIT="$(tr -d '[:space:]' < "$HERE/KICANVAS_COMMIT")"
SRC="$HERE/.cache/kicanvas"
if [ ! -d "$SRC/.git" ]; then
  git clone --filter=blob:none https://github.com/theacodes/kicanvas.git "$SRC"
fi
git -C "$SRC" fetch --quiet origin
git -C "$SRC" checkout --quiet --detach "$COMMIT"
(cd "$SRC" && npm ci --include=dev --no-audit --no-fund && npm run build)
mkdir -p "$HERE/vendor"
cp "$SRC/build/kicanvas.js" "$HERE/vendor/kicanvas.js"
cp "$SRC/LICENSE.md" "$HERE/vendor/LICENSE.md"
{
  echo "commit=$COMMIT"
  echo "commit_date=$(git -C "$SRC" show -s --format=%cI HEAD)"
  echo "node=$(node --version) npm=$(npm --version)"
  echo "size=$(stat -c %s "$HERE/vendor/kicanvas.js")"
  echo "sha256=$(sha256sum "$HERE/vendor/kicanvas.js" | cut -d' ' -f1)"
} > "$HERE/vendor/BUILD_INFO"
cat "$HERE/vendor/BUILD_INFO"
