#!/usr/bin/env bash
# Usage: ./run.sh q01_create.py   (runs inside the pinned kicad-base image; results/ is written here)
set -euo pipefail
cd "$(dirname "$0")"
IMAGE="${KICAD_BASE_IMAGE:-ghcr.io/peterus/copperchat-kicad-base:52b43fb8fc8e11fdab5d683eaefc91a49f9cbcd7-rf96c7c8335ad9c6f}"
mkdir -p results
# An image built before the kipy fork pin ships upstream kipy 76b47d25 (issue #147: create/update_items
# raise AttributeError type_url). Then overlay .deps/kipy = that kipy + the fork's fix (ab07e98).
OLD_KIPY=76b47d25a826a56a301660f5f572dda968c84d59
PYPATH=/spike
if [ "$(docker inspect "$IMAGE" --format '{{index .Config.Labels "io.copperchat.kipy-revision"}}')" = "$OLD_KIPY" ]; then
  # overlay only for such an image; the dir is keyed by the image's kipy revision
  DEPS=".deps/$OLD_KIPY"
  if [ ! -d "$DEPS/kipy" ]; then
    mkdir -p "$DEPS"
    docker run --rm --user "$(id -u):$(id -g)" --entrypoint sh -v "$PWD/$DEPS:/deps" "$IMAGE" \
      -c 'cp -r /opt/venv/lib/python3.13/site-packages/kipy /deps/'
    sed -i 's/self\._item_from_message(unpack_any(result\.item))/self._item_from_message(result.item)/' "$DEPS/kipy/editor.py"
    test "$(grep -c '_item_from_message(result.item)' "$DEPS/kipy/editor.py")" = 2
  fi
  PYPATH="/spike/$DEPS:/spike"
fi
# --init: python is not PID 1, so `timeout` (run_all.sh) can stop a hung probe and its api-servers
exec docker run --rm --init --user "$(id -u):$(id -g)" --entrypoint /opt/venv/bin/python \
  -e PYTHONPATH="$PYPATH" -e SPIKE_RESULTS=/spike/results -e PYTHONDONTWRITEBYTECODE=1 -e HOME=/tmp \
  -v "$PWD:/spike" -w /spike "$IMAGE" "/spike/$@"
