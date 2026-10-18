#!/usr/bin/env bash
# Asserts the vendored KiCanvas bundle is present, MIT licensed, pinned, built from the
# KiCanvas fork (peterus/kicanvas) and offline (no Google Fonts request).
set -euo pipefail
FRONTEND="$(cd "$(dirname "$0")/.." && pwd)"
V="$FRONTEND/public/vendor/kicanvas"
fail() { echo "FAIL: $*" >&2; exit 1; }
[ -s "$V/kicanvas.js" ] || fail "kicanvas.js missing"
[ -s "$V/KICANVAS_COMMIT" ] || fail "KICANVAS_COMMIT missing"
grep -q "MIT License" "$V/LICENSE.md" || fail "LICENSE.md is not the MIT license"
grep -qx "commit=$(tr -d '[:space:]' < "$V/KICANVAS_COMMIT")" "$V/BUILD_INFO" || fail "BUILD_INFO does not match KICANVAS_COMMIT"
grep -qx "repo=https://github.com/peterus/kicanvas.git" "$V/BUILD_INFO" || fail "BUILD_INFO repo is not the peterus/kicanvas fork"
grep -Eqx '[0-9a-f]{40}' "$V/KICANVAS_COMMIT" || fail "KICANVAS_COMMIT is not a full commit SHA"
grep -qx "sha256=$(sha256sum "$V/kicanvas.js" | cut -d' ' -f1)" "$V/BUILD_INFO" || fail "kicanvas.js does not match the sha256 in BUILD_INFO"
grep -q 'kicanvas-embed' "$V/kicanvas.js" || fail "bundle does not define kicanvas-embed"
grep -q 'kicanvas:viewerload' "$V/kicanvas.js" || fail "bundle lacks the embed host integration (not built from the copperchat branch?)"
grep -q 'clear_highlight' "$V/kicanvas.js" || fail "bundle lacks the schematic highlight overlay (E-C13)"
if grep -Eq 'fonts\.(googleapis|gstatic)\.com' "$V/kicanvas.js"; then fail "bundle still loads Google Fonts"; fi
echo "PASS: kicanvas vendored ($(stat -c %s "$V/kicanvas.js") bytes, commit $(cat "$V/KICANVAS_COMMIT"))"
