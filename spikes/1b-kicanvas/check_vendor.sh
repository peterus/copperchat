#!/usr/bin/env bash
# Asserts the vendored KiCanvas bundle exists, is MIT licensed, and reports size.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
V="$HERE/vendor"
fail() { echo "FAIL: $*"; exit 1; }
[ -s "$V/kicanvas.js" ] || fail "vendor/kicanvas.js missing"
[ -s "$V/LICENSE.md" ] || fail "vendor/LICENSE.md missing"
grep -q "available under the MIT License" "$V/LICENSE.md" || fail "LICENSE.md is not MIT"
grep -q "customElements.define(\"kicanvas-embed\"" "$V/kicanvas.js" \
  || grep -q 'kicanvas-embed' "$V/kicanvas.js" || fail "bundle does not define kicanvas-embed"
for ev in kicanvas:select kicanvas:load; do
  grep -q "$ev" "$V/kicanvas.js" || fail "event name $ev not in bundle"
done
if grep -q 'kicanvas:click' "$V/kicanvas.js"; then echo "NOTE: bundle mentions kicanvas:click"; \
  else echo "NOTE: no kicanvas:click event in bundle"; fi
size=$(stat -c %s "$V/kicanvas.js")
gz=$(gzip -9c "$V/kicanvas.js" | wc -c)
sha=$(sha256sum "$V/kicanvas.js" | cut -d' ' -f1)
fonts=$(grep -o 'fonts.googleapis.com[^"`]*' "$V/kicanvas.js" | head -1 || true)
echo "PASS: vendor ok size=${size}B gzip=${gz}B sha256=${sha}"
echo "NOTE: external font URL in bundle: ${fonts:-none}"
