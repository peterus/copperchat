#!/usr/bin/env bash
# scripts/kicad-base-image.sh — names of the KiCad base image (run from the repo root).
#
#   recipe            16-hex hash of everything that shapes the image: docker/kicad-base/*
#                     (except SUPPORTED_COMMITS, which only feeds CI matrices) and the
#                     workflow that builds it.
#   tag <commit>      the recipe-pinned tag <commit>-r<recipe>. kicad-base.yml pushes it from
#                     every same-repo run that builds; it is never overwritten (same recipe,
#                     same image), so a pull request can publish and test a new recipe
#                     without touching the published <commit> tag.
#   resolve <commit>  the image CI should use for <commit>. For the pinned KICAD_COMMIT it
#                     must match the current recipe: the recipe-pinned tag if it exists, else
#                     <commit> if its io.copperchat.recipe label matches; otherwise it fails
#                     (the kicad-base workflow has not published this recipe yet). Other
#                     SUPPORTED_COMMITS resolve to their published <commit> tag as is.
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
  resolve)
    commit="${2:?usage: $0 resolve <commit>}"
    pinned="$(tr -d '[:space:]' < docker/kicad-base/KICAD_COMMIT)"
    if [ "$commit" != "$pinned" ]; then echo "$IMAGE:$commit"; exit 0; fi
    r="$(recipe)"
    rc=0; label "$IMAGE:$commit-r$r" >/dev/null || rc=$?
    if [ "$rc" = 0 ]; then echo "$IMAGE:$commit-r$r"; exit 0; fi
    [ "$rc" = 1 ] || exit "$rc"
    rc=0; published="$(label "$IMAGE:$commit")" || rc=$?
    [ "$rc" = 0 ] || [ "$rc" = 1 ] || exit "$rc"
    if [ "$rc" = 0 ] && [ "$published" = "$r" ]; then echo "$IMAGE:$commit"; exit 0; fi
    echo "kicad-base for KiCad $commit with recipe $r is not published yet: wait for the" \
      "kicad-base workflow (it re-runs this workflow's failed jobs when it has pushed)" >&2
    exit 1 ;;
  *) echo "usage: $0 recipe | tag <commit> | label <image> | resolve <commit>" >&2; exit 64 ;;
esac
