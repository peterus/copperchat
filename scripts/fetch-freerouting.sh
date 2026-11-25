#!/usr/bin/env bash
# scripts/fetch-freerouting.sh DEST — install the checksum-pinned Freerouting JAR and Temurin JRE
# from docker/freerouting.lock (FREEROUTING_LOCK overrides the lock path) into DEST:
#   DEST/jre/bin/java, DEST/freerouting-<version>.jar, DEST/.lock, DEST/.manifest.sha256.
# Used by docker/app/Dockerfile and the CI kicad job (whose actions/cache restores DEST).
# Nothing found in DEST is trusted: the kept downloads (DEST/.dl/) are re-verified against the
# lock on every run (a mismatch is deleted and downloaded again; a fresh download that mismatches
# fails the run), and the installed tree is re-installed from them unless it matches the
# manifest written at install time. The manifest is written last; it only exists for a tree
# installed completely from verified downloads. Downloads use python3 urllib (no curl in the
# kicad-base image); file:// URLs work too (tests). Total download time is bounded by 300 s.
set -euo pipefail
umask 022

die() {
  echo "fetch-freerouting: ERROR: $*" >&2
  exit 1
}

if [ "$#" -ne 1 ] || [ -z "$1" ]; then die "usage: fetch-freerouting.sh DEST"; fi
lock="${FREEROUTING_LOCK:-$(dirname -- "${BASH_SOURCE[0]}")/../docker/freerouting.lock}"
[ -f "$lock" ] || die "lock file not found: $lock"
lock="$(cd -- "$(dirname -- "$lock")" && pwd -P)/$(basename -- "$lock")"

# Parse (never execute) the lock: exactly the five known KEY=value lines.
FREEROUTING_VERSION="" FREEROUTING_URL="" FREEROUTING_SHA256="" TEMURIN_URL="" TEMURIN_SHA256=""
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in '' | '#'*) continue ;; esac
  [[ "$line" =~ ^([A-Z0-9_]+)=([^[:space:]\"\'\`\$\;]+)$ ]] || die "malformed lock line: $line"
  key="${BASH_REMATCH[1]}" value="${BASH_REMATCH[2]}"
  case "$key" in
    FREEROUTING_VERSION | FREEROUTING_URL | FREEROUTING_SHA256 | TEMURIN_URL | TEMURIN_SHA256)
      [ -z "${!key}" ] || die "duplicate lock key: $key"
      printf -v "$key" '%s' "$value"
      ;;
    *) die "unknown lock key: $key" ;;
  esac
done <"$lock"
[[ "$FREEROUTING_VERSION" =~ ^[0-9]+(\.[0-9]+)*$ ]] || die "bad FREEROUTING_VERSION in $lock"
for key in FREEROUTING_SHA256 TEMURIN_SHA256; do
  [[ "${!key}" =~ ^[0-9a-f]{64}$ ]] || die "bad $key in $lock (want 64 lowercase hex digits)"
done
for key in FREEROUTING_URL TEMURIN_URL; do
  [[ "${!key}" =~ ^(https|file)://[^?#]+$ ]] || die "bad $key in $lock (want https:// or file://)"
done

jar_name="freerouting-${FREEROUTING_VERSION}.jar"
jre_name="${TEMURIN_URL##*/}"
if ! [[ "$jre_name" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*$ ]] || [ "$jre_name" = "$jar_name" ]; then
  die "bad TEMURIN_URL file name: $jre_name"
fi

mkdir -p -- "$1"
dest="$(cd -- "$1" && pwd -P)"
dl="$dest/.dl"
dl_tmp="$dest/.dl.tmp"
jre_tmp="$dest/.jre.tmp"
manifest="$dest/.manifest.sha256"

cleanup() {
  local rc=$?
  rm -rf -- "$dl_tmp" "$jre_tmp" "$dest/.$jar_name.tmp" "$manifest.tmp" "$dest/.lock.tmp"
  if [ "$rc" -ne 0 ]; then
    rm -f -- "$manifest"
    rmdir -- "$dl" 2>/dev/null || true
  fi
  exit "$rc"
}
trap cleanup EXIT

sha256_of() { sha256sum -- "$1" | cut -d' ' -f1; }

deadline=$((SECONDS + 300))

download() { # URL OUT
  local remaining=$((deadline - SECONDS))
  [ "$remaining" -gt 0 ] || die "download time limit (300 s) exhausted before $1"
  timeout --kill-after=5 "$remaining" python3 -I -c '
import shutil, sys, urllib.request
url, out, limit = sys.argv[1], sys.argv[2], float(sys.argv[3])
with urllib.request.urlopen(url, timeout=min(limit, 60.0)) as r, open(out, "xb") as f:
    shutil.copyfileobj(r, f, 1 << 20)
' "$1" "$2" "$remaining" || die "download failed (or exceeded the 300 s limit): $1"
}

# 1. Verified downloads in .dl/ (kept: the CI cache restores them).
rm -rf -- "$dl_tmp"
mkdir -p -- "$dl"
find "$dl" -mindepth 1 -maxdepth 1 ! -name "$jar_name" ! -name "$jre_name" -exec rm -rf -- {} +

ensure_download() { # LABEL NAME URL SHA256
  local label="$1" name="$2" url="$3" want="$4" got
  if [ -e "$dl/$name" ]; then
    if [ -f "$dl/$name" ] && [ ! -L "$dl/$name" ] \
      && printf '%s  %s\n' "$want" "$dl/$name" | sha256sum -c --quiet --strict - >/dev/null 2>&1; then
      return 0
    fi
    got="$( [ -f "$dl/$name" ] && sha256_of "$dl/$name" || echo "not a regular file")"
    echo "fetch-freerouting: cached download .dl/$name ($label) does not match the lock:" \
      "expected $want, actual $got; deleting it and downloading again" >&2
    rm -rf -- "${dl:?}/$name"
  fi
  mkdir -p -- "$dl_tmp"
  echo "fetch-freerouting: downloading $label: $url"
  download "$url" "$dl_tmp/$name"
  got="$(sha256_of "$dl_tmp/$name")"
  [ "$got" = "$want" ] \
    || die "downloaded $name ($label) from $url does not match the lock: expected $want, actual $got"
  mv -f -- "$dl_tmp/$name" "$dl/$name"
}

ensure_download "Freerouting $FREEROUTING_VERSION JAR" "$jar_name" "$FREEROUTING_URL" "$FREEROUTING_SHA256"
ensure_download "Temurin JRE" "$jre_name" "$TEMURIN_URL" "$TEMURIN_SHA256"
rmdir -- "$dl_tmp" 2>/dev/null || true

# 2. Installed tree: up to date only if .lock equals the lock and the manifest matches exactly.
tree_ok() {
  [ -f "$manifest" ] && [ -f "$dest/.lock" ] && cmp -s -- "$lock" "$dest/.lock" || return 1
  grep -qxF -- "$FREEROUTING_SHA256  $jar_name" "$manifest" || return 1
  [ -d "$dest/jre" ] && [ ! -L "$dest/jre" ] || return 1
  (cd -- "$dest" && sha256sum -c --quiet --strict .manifest.sha256 >/dev/null 2>&1) || return 1
  # no file under jre/ that the manifest does not list
  diff -q <(cd -- "$dest" && { find jre -type f; echo "$jar_name"; } | LC_ALL=C sort) \
    <(cut -d' ' -f3- "$manifest" | LC_ALL=C sort) >/dev/null
}

if tree_ok; then
  echo "fetch-freerouting: up to date: Freerouting $FREEROUTING_VERSION + JRE in $dest"
  exit 0
fi

echo "fetch-freerouting: installing Freerouting $FREEROUTING_VERSION + JRE into $dest"
rm -f -- "$manifest" "$dest/.lock"
rm -rf -- "$dest/jre" "$jre_tmp"
find "$dest" -mindepth 1 -maxdepth 1 -name 'freerouting-*.jar' -exec rm -f -- {} +

# Extract into a fresh directory, dropping the tarball's single top-level directory
# (--strip-components=1); tarfile's "data" filter refuses absolute paths, "..", links that
# leave the tree and device files, and drops owners and setuid bits.
python3 -I -c '
import sys, tarfile
src, out = sys.argv[1], sys.argv[2]
with tarfile.open(src, "r:gz") as tar:
    members = tar.getmembers()
    tops = {m.name.split("/", 1)[0] for m in members}
    if len(tops) != 1 or tops & {"", ".", ".."}:
        sys.exit(f"unexpected tarball layout (top-level entries: {sorted(tops)[:5]})")
    top = tops.pop() + "/"
    keep = []
    for m in members:
        rest = m.name[len(top):] if m.name.startswith(top) else ""
        if not rest.strip("/"):
            continue
        m.name = rest
        if m.islnk():
            if not m.linkname.startswith(top):
                sys.exit(f"hard link outside the tarball top directory: {m.linkname}")
            m.linkname = m.linkname[len(top):]
        keep.append(m)
    tar.extractall(out, members=keep, filter="data")
' "$dl/$jre_name" "$jre_tmp" || die "cannot extract $jre_name"
mv -T -- "$jre_tmp" "$dest/jre"
[ -x "$dest/jre/bin/java" ] || die "$jre_name has no bin/java"

cp -- "$dl/$jar_name" "$dest/.$jar_name.tmp"
mv -f -- "$dest/.$jar_name.tmp" "$dest/$jar_name"

cp -- "$lock" "$dest/.lock.tmp"
mv -f -- "$dest/.lock.tmp" "$dest/.lock"
(cd -- "$dest" && { find jre -type f -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum --; sha256sum -- "$jar_name"; }) \
  >"$manifest.tmp"
mv -f -- "$manifest.tmp" "$manifest"
echo "fetch-freerouting: installed Freerouting $FREEROUTING_VERSION + JRE into $dest"
