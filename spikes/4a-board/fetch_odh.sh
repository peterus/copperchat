#!/usr/bin/env bash
# Copies the OpenDriveHub transmitter main project (schematics + project + local footprint library)
# into fixtures/odh/ (gitignored). Read-only on the source; never write to it.
set -euo pipefail
cd "$(dirname "$0")"
SRC="${ODH_SRC:-$HOME/copperchat-run/projects/OpenDriveHub}"
DST=fixtures/odh
rm -rf "$DST"
mkdir -p "$DST/hardware/transmitter/pcb/main" "$DST/footprints"
cp "$SRC"/hardware/transmitter/pcb/main/*.kicad_{sch,pro,pcb} "$SRC"/hardware/transmitter/pcb/main/fp-lib-table \
  "$DST/hardware/transmitter/pcb/main/"
cp -r "$SRC/footprints/OpenDriveHub.pretty" "$DST/footprints/"
git -C "$SRC" rev-parse HEAD > "$DST/SOURCE_REV"
echo "copied $(ls "$DST/hardware/transmitter/pcb/main" | wc -l) files @ $(cat "$DST/SOURCE_REV")"
