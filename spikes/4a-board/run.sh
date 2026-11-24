#!/usr/bin/env bash
# Usage: ./run.sh q01_netlist.py   (runs inside the pinned kicad-base image; results/ is written here)
set -euo pipefail
cd "$(dirname "$0")"
IMAGE="${KICAD_BASE_IMAGE:-ghcr.io/peterus/copperchat-kicad-base:52b43fb8fc8e11fdab5d683eaefc91a49f9cbcd7-r98c78374f0daab9d}"
mkdir -p results
# --init: python is not PID 1, so `timeout` (run_all.sh) can stop a hung probe and its api-servers
rc=0
docker run --rm --init --user "$(id -u):$(id -g)" --entrypoint /opt/venv/bin/python \
  -e PYTHONPATH=/spike -e SPIKE_RESULTS=/spike/results -e PYTHONDONTWRITEBYTECODE=1 -e HOME=/tmp \
  -v "$PWD:/spike" -w /spike "$IMAGE" "/spike/$@" || rc=$?
# rasterise renders (kicad-cli writes board-area SVGs; the image has no rasteriser) — host ImageMagick
for svg in results/*.svg; do
  [ -e "$svg" ] || continue
  convert -density 400 -background '#001023' "$svg" -flatten -trim +repage -bordercolor '#001023' -border 20 -resize '1600x1600>' -colors 256 "PNG8:${svg%.svg}.png" && rm -f "$svg"
done
exit $rc
