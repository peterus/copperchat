#!/usr/bin/env bash
# scripts/test-kicad-base-image.sh — tests scripts/kicad-base-image.sh (resolve, published,
# decide) against a stubbed registry: a fake `docker` that answers `buildx imagetools
# inspect` from $STUB_REGISTRY ("<tag> <recipe label>" lines; a tag listed as "<tag> !"
# simulates a registry error). Run from the repo root.
set -euo pipefail
SCRIPT="$PWD/scripts/kicad-base-image.sh"
IMAGE=ghcr.io/peterus/copperchat-kicad-base
COMMIT="$(tr -d '[:space:]' < docker/kicad-base/KICAD_COMMIT)"
RECIPE="$(bash "$SCRIPT" recipe)"
OTHER=1111111111111111111111111111111111111111

stub="$(mktemp -d)"; trap 'rm -rf "$stub"' EXIT
cat > "$stub/docker" <<'EOF'
#!/usr/bin/env bash
ref="$4"   # docker buildx imagetools inspect <ref> --format ...
while read -r tag label; do
  [ "$tag" = "$ref" ] || continue
  if [ "$label" = "!" ]; then echo "ERROR: 503 Service Unavailable" >&2; exit 1; fi
  printf '{"config":{"Labels":{"io.copperchat.recipe":"%s"}}}\n' "$label"; exit 0
done < "$STUB_REGISTRY"
echo "ERROR: $ref: not found" >&2; exit 1
EOF
chmod +x "$stub/docker"
export PATH="$stub:$PATH" STUB_REGISTRY="$stub/registry"

fails=0
check() {  # check <name> <expected> <actual>
  if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "FAIL $1: expected '$2', got '$3'"; fails=$((fails + 1)); fi
}
registry() { printf '%s\n' "$@" > "$STUB_REGISTRY"; }
run() { local out rc=0; out="$("$@" 2>/dev/null)" || rc=$?; echo "$rc:$(tr '\n' ' ' <<<"$out" | sed 's/ $//')"; }
decide() { run env COMMIT="$COMMIT" FORCE="$1" REF="$2" SAME_REPO="$3" bash "$SCRIPT" decide; }

pub="$IMAGE:$COMMIT"; rtag="$IMAGE:$COMMIT-r$RECIPE"
none="build=false push_rtag=false push_commit=false"

# resolve / published
registry "$pub $RECIPE"
check "resolve: published tag with current recipe" "0:$pub" "$(run bash "$SCRIPT" resolve "$COMMIT")"
check "published: label matches" "0:$pub" "$(run bash "$SCRIPT" published "$COMMIT")"
registry "$pub oldrecipe" "$rtag $RECIPE"
check "resolve: falls back to the recipe-pinned tag" "0:$rtag" "$(run bash "$SCRIPT" resolve "$COMMIT")"
check "published: never the recipe-pinned tag" "1:" "$(run bash "$SCRIPT" published "$COMMIT")"
registry "$pub oldrecipe"
check "resolve: recipe not built yet" "1:" "$(run bash "$SCRIPT" resolve "$COMMIT")"
registry "$pub !"
check "resolve: registry error is not 'missing'" "2:" "$(run bash "$SCRIPT" resolve "$COMMIT")"
check "resolve: other supported commit as is" "0:$IMAGE:$OTHER" "$(run bash "$SCRIPT" resolve "$OTHER")"

# decide: main
registry "$pub oldrecipe"
check "main, new recipe: build + push both" "0:build=true push_rtag=true push_commit=true" "$(decide false refs/heads/main true)"
registry "$pub oldrecipe" "$rtag $RECIPE"
check "main, branch already built the recipe: still rebuilds" "0:build=true push_rtag=true push_commit=true" "$(decide false refs/heads/main true)"
registry "$pub $RECIPE"
check "main, published: nothing" "0:$none" "$(decide false refs/heads/main true)"
check "main, published + force: rebuild" "0:build=true push_rtag=true push_commit=true" "$(decide true refs/heads/main true)"
registry
check "main, new commit: build + push both" "0:build=true push_rtag=true push_commit=true" "$(decide false refs/heads/main true)"

# decide: branches
registry "$pub oldrecipe"
check "branch, new recipe: build + rtag only" "0:build=true push_rtag=true push_commit=false" "$(decide false refs/pull/1/merge true)"
check "fork, new recipe: build, no push" "0:build=true push_rtag=false push_commit=false" "$(decide false refs/pull/1/merge false)"
registry
check "branch, new commit: never pushes <commit>" "0:build=true push_rtag=true push_commit=false" "$(decide false refs/pull/1/merge true)"
registry "$pub oldrecipe" "$rtag $RECIPE"
check "branch, recipe tag exists: nothing" "0:$none" "$(decide false refs/pull/1/merge true)"
check "branch, force: build, never overwrites" "0:build=true push_rtag=false push_commit=false" "$(decide true refs/pull/1/merge true)"
registry "$pub $RECIPE"
check "branch, published recipe: nothing" "0:$none" "$(decide false refs/pull/1/merge true)"
registry "$pub oldrecipe" "$rtag !"
check "decide: registry error aborts" "1:" "$(decide false refs/heads/main true)"

[ "$fails" = 0 ] || { echo "$fails failure(s)"; exit 1; }
echo "all kicad-base tag tests passed"
