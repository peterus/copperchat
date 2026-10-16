#!/usr/bin/env bash
# Checks out kicad-python at the pinned revision into .kicad/kicad-python with a shallow
# KiCad submodule (the protos). pyproject.toml uses it as a path source; a plain uv git
# source works too but clones the whole KiCad repository (minutes, ~3.5 GB cache).
set -euo pipefail
cd "$(dirname "$0")"
REV="${KIPY_REV:-87b945a0d56b26a78bf547db9771ff9bf3a057ea}"
mkdir -p .kicad
if [ ! -d .kicad/kicad-python/.git ]; then
  git clone -q https://gitlab.com/kicad/code/kicad-python.git .kicad/kicad-python
fi
cd .kicad/kicad-python
git fetch -q origin
git checkout -q "$REV"
git submodule update --init --depth 1
echo "kicad-python: $(git rev-parse HEAD)"
echo "kicad submodule (protos): $(git -C kicad rev-parse HEAD)"
