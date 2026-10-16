#!/usr/bin/env bash
# Download and extract the latest KiCad nightly (lite) AppImage; no install, no FUSE.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CACHE="$HERE/.cache/kicad-nightly"
INDEX="https://downloads.kicad.org/kicad/linux/explore/nightlies"
mkdir -p "$CACHE" "$HERE/.cache/bin"
url="${KICAD_APPIMAGE_URL:-$(curl -fsSL "$INDEX" \
  | grep -oE 'https://[^"]*/kicad-nightly-[^"]*-x86_64-lite\.AppImage\.tar' | sort -uV | tail -1)}"
[ -n "$url" ] || { echo "FAIL: no lite AppImage link at $INDEX"; exit 1; }
file="$(basename "$url")"
[ -f "$CACHE/$file" ] || curl -fL --retry 3 -o "$CACHE/$file" "$url"
rm -rf "$CACHE/x" && mkdir -p "$CACHE/x"
tar -xf "$CACHE/$file" -C "$CACHE/x"
appimage="$(find "$CACHE/x" -maxdepth 2 -name '*.AppImage' | head -1)"
chmod +x "$appimage"
(cd "$CACHE/x" && "$appimage" --appimage-extract >/dev/null)
root="$CACHE/x/squashfs-root"

# Mode A: run the binary directly with the AppImage's libraries.
cat > "$HERE/.cache/bin/kicad-cli" <<EOF
#!/usr/bin/env bash
export LD_LIBRARY_PATH="$root/usr/lib:$root/usr/lib/x86_64-linux-gnu\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
exec "$root/usr/bin/kicad-cli" "\$@"
EOF
chmod +x "$HERE/.cache/bin/kicad-cli"
if ! "$HERE/.cache/bin/kicad-cli" version >/dev/null 2>&1; then
  # Mode B: let AppRun set up the environment.
  printf '#!/usr/bin/env bash\nexec "%s/AppRun" kicad-cli "$@"\n' "$root" > "$HERE/.cache/bin/kicad-cli"
  "$HERE/.cache/bin/kicad-cli" version >/dev/null || { echo "FAIL: kicad-cli does not start"; exit 1; }
  echo "NOTE: using AppRun mode"
fi

version="$(echo "$file" | sed -E 's/^kicad-nightly-(.*)-x86_64-lite\.AppImage\.tar$/\1/')"
short="$(echo "$version" | sed -E 's/.*\.g([0-9a-f]+)$/\1/')"
read -r commit commit_date < <(curl -fsSL \
  "https://gitlab.com/api/v4/projects/kicad%2Fcode%2Fkicad/repository/commits/$short" \
  | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["id"], d["committed_date"])')
cat > "$CACHE/nightly.env" <<EOF
NIGHTLY_FILE=$file
NIGHTLY_VERSION=$version
NIGHTLY_SHORT=$short
NIGHTLY_COMMIT=$commit
NIGHTLY_COMMIT_DATE=$commit_date
EOF
cat "$CACHE/nightly.env"
"$HERE/.cache/bin/kicad-cli" version
