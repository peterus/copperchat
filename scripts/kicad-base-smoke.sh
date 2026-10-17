#!/usr/bin/env bash
# Smoke test for ghcr.io/peterus/copperchat-kicad-base images.
# Usage: scripts/kicad-base-smoke.sh <image>
set -euo pipefail

IMAGE="${1:?usage: $0 <image>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXPECTED_COMMIT="$(tr -d '[:space:]' < "$ROOT/docker/kicad-base/KICAD_COMMIT")"
SHORT="${EXPECTED_COMMIT:0:10}"
fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
run() { docker run --rm --env LANG=C --env LC_ALL=C "$IMAGE" "$@"; }   # no DISPLAY passed, no X socket

echo "== image: $IMAGE (expect KiCad $SHORT)"

uid="$(run id -u)" || fail "image does not start"
[ "$uid" = "1000" ] || fail "runs as uid $uid, expected 1000 (copperchat)"
[ "$(run id -un)" = "copperchat" ] || fail "user is not copperchat"

ver="$(run kicad-cli version --format about)" || fail "kicad-cli version failed"
echo "$ver" | head -n 5
grep -q "$SHORT" <<<"$ver" || fail "kicad-cli version does not mention commit $SHORT"

help="$(run kicad-cli api-server --help 2>&1)" || fail "kicad-cli api-server --help failed"
grep -q -- "--socket" <<<"$help" || fail "api-server --help lacks --socket"

run sh -c 'test -z "${DISPLAY:-}" && ! command -v Xvfb' \
  || fail "DISPLAY set or Xvfb installed (must run headless without X)"

run sh -c '! command -v gcc && ! command -v cc && ! command -v cmake' >/dev/null \
  || fail "toolchain found in runtime image"

pkgs="$(run dpkg-query -W -f '${binary:Package}\n')" || fail "dpkg-query failed"
dev="$(grep -E -- '-dev(:[a-z0-9]+)?$' <<<"$pkgs" || true)"
[ -z "$dev" ] || fail "-dev packages in runtime image: $(tr '\n' ' ' <<<"$dev")"

run git --version >/dev/null || fail "git missing (X12: runtime keeps git)"

run python3 -c "import kipy.schematic, kipy.board_jobs; print('kipy imports ok')" \
  || fail "kipy.schematic / kipy.board_jobs import failed"

run sh -c 'test -d /usr/share/kicad/symbols && test -d /usr/share/kicad/footprints && test -d /usr/share/kicad/demos/ecc83' \
  || fail "symbol/footprint libraries or demos missing"

run sh -c 'test -n "${KICAD_CONFIG_HOME:-}" && ls "$KICAD_CONFIG_HOME"/*/sym-lib-table "$KICAD_CONFIG_HOME"/*/fp-lib-table' \
  || fail "global sym-lib-table/fp-lib-table missing under KICAD_CONFIG_HOME"

# With the global library tables in place, ERC on the stock demo must resolve the
# stock footprint libraries (docs/spikes/1c-api-server.md, Q6). Runs on a copy.
erc="$(run sh -c 'set -e; d="$(mktemp -d)"; cp -r /usr/share/kicad/demos/ecc83 "$d/";
  kicad-cli sch erc --format json --units mm -o "$d/erc.json" "$d/ecc83/ecc83-pp.kicad_sch" >/dev/null;
  cat "$d/erc.json"')" || fail "kicad-cli sch erc failed"
grep -q '"violations"' <<<"$erc" || fail "ERC report has no violations array"
if grep -q 'configuration does not include the footprint library' <<<"$erc"; then
  fail "ERC: stock footprint libraries not in the global fp-lib-table"
fi

run python3 /opt/copperchat/smoke_ping.py \
  || fail "api-server did not start or kipy ping failed"

echo "SMOKE OK"
