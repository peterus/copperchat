#!/usr/bin/env bash
# scripts/kicad-base-image.sh — names of the KiCad base image (run from the repo root).
#
#   recipe            16-hex hash of everything that shapes the image: docker/kicad-base/*
#                     (except SUPPORTED_COMMITS, which only feeds CI matrices) and the
#                     workflow that builds it.
#   tag <commit>      the recipe-pinned tag <commit>-r<recipe>, the image a pull request
#                     built for its recipe (kicad-base.yml). Only CI test jobs use it.
#   resolve <commit>  the image CI tests <commit> against. For the pinned KICAD_COMMIT it must
#                     have the current recipe: <commit> if its io.copperchat.recipe label
#                     matches, else the recipe-pinned tag; otherwise it fails (kicad-base has
#                     not published this recipe yet). Other SUPPORTED_COMMITS resolve to their
#                     published <commit> tag as is.
#   published <commit> <commit> if its label matches the current recipe, else fail. For
#                     releases: never a pull request's recipe-pinned build.
set -euo pipefail
IMAGE="${KICAD_BASE_REPO:-ghcr.io/peterus/copperchat-kicad-base}"

recipe() {
  { git ls-files docker/kicad-base | grep -v '/SUPPORTED_COMMITS$'; echo .github/workflows/kicad-base.yml; } \
    | LC_ALL=C sort | xargs sha256sum | sha256sum | cut -c1-16
}

# Prints the recipe label of an image, "" if it has none; exit 1 if the tag does not
# exist, 2 on any other registry error (never mistake an outage for "missing").
label() {
  local json err
  err="$(mktemp)"
  if json="$(docker buildx imagetools inspect "$1" --format '{{json .Image}}' 2>"$err")"; then
    rm -f "$err"
    jq -r 'if has("config") then .config.Labels["io.copperchat.recipe"]
      else .["linux/amd64"].config.Labels["io.copperchat.recipe"] end // ""' <<<"$json"
  elif grep -qiE 'not found|manifest unknown' "$err"; then
    rm -f "$err"; return 1
  else
    cat "$err" >&2; rm -f "$err"; echo "imagetools inspect $1 failed" >&2; return 2
  fi
}

case "${1:-}" in
  recipe) recipe ;;
  tag)
    commit="${2:?usage: $0 tag <commit>}"
    echo "$IMAGE:$commit-r$(recipe)" ;;
  label) label "${2:?usage: $0 label <image>}" ;;
  resolve|published)
    commit="${2:?usage: $0 $1 <commit>}"
    pinned="$(tr -d '[:space:]' < docker/kicad-base/KICAD_COMMIT)"
    if [ "$1" = resolve ] && [ "$commit" != "$pinned" ]; then echo "$IMAGE:$commit"; exit 0; fi
    r="$(recipe)"
    rc=0; published="$(label "$IMAGE:$commit")" || rc=$?
    [ "$rc" = 0 ] || [ "$rc" = 1 ] || exit "$rc"
    if [ "$rc" = 0 ] && [ "$published" = "$r" ]; then echo "$IMAGE:$commit"; exit 0; fi
    if [ "$1" = resolve ]; then
      rc=0; label "$IMAGE:$commit-r$r" >/dev/null || rc=$?
      if [ "$rc" = 0 ]; then echo "$IMAGE:$commit-r$r"; exit 0; fi
      [ "$rc" = 1 ] || exit "$rc"
      echo "kicad-base for KiCad $commit with recipe $r is not published yet: wait for the" \
        "kicad-base workflow (it re-runs this commit's failed CI jobs when it has pushed)" >&2
    else
      echo "$IMAGE:$commit has recipe '$published', not $r: wait for kicad-base on main" >&2
    fi
    exit 1 ;;
  *) echo "usage: $0 recipe | tag <commit> | label <image> | resolve <commit> | published <commit>" >&2; exit 64 ;;
esac
