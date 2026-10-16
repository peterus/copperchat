#!/usr/bin/env bash
# Downloads the newest KiCad nightly AppImage, extracts it, prints the full commit.
set -euo pipefail
cd "$(dirname "$0")"
BASE="https://downloads.kicad.org/kicad/linux/explore/nightlies"
mkdir -p .kicad && cd .kicad
NAME="${KICAD_NIGHTLY_NAME:-$(curl -fsSL "$BASE/" \
  | grep -oE 'kicad-nightly-10\.99\.0\.[0-9]+\.g[0-9a-f]+-x86_64\.AppImage\.tar' \
  | sort -t. -k4,4n -u | tail -n1)}"
echo "nightly: $NAME"
[ -f "$NAME" ] || curl -fL -o "$NAME" "$BASE/download/$NAME"
rm -rf squashfs-root ./*.AppImage
tar -xf "$NAME"
APPIMAGE="$(ls ./*.AppImage | head -n1)"
chmod +x "$APPIMAGE"
"$APPIMAGE" --appimage-extract >/dev/null
SHORT="$(echo "$NAME" | sed -E 's/.*\.g([0-9a-f]+)-x86_64.*/\1/')"
FULL="$(curl -fsSL "https://gitlab.com/api/v4/projects/kicad%2Fcode%2Fkicad/repository/commits/$SHORT" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
printf '%s\n%s\n' "$NAME" "$FULL" > nightly.txt
echo "full commit: $FULL"
ls -l squashfs-root/AppRun
