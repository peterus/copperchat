#!/usr/bin/env bash
# scripts/app-image-tools.sh IMAGE — external binaries the agent needs at runtime are on PATH
# for the app user: pdftoppm (the Claude CLI's Read tool renders PDF pages with it).
set -euo pipefail
image="${1:?usage: app-image-tools.sh IMAGE}"
timeout 120 docker run --rm --user 1000:1000 --entrypoint bash "$image" -euc '
  command -v pdftoppm || { echo "FAIL: pdftoppm not on PATH"; exit 1; }
  pdftoppm -v
  cd "$(mktemp -d)"
  python -c "from PIL import Image; Image.new(\"RGB\", (200, 100), \"white\").save(\"sample.pdf\")"
  pdftoppm -png -f 1 -l 1 sample.pdf page
  ls page*.png >/dev/null || { echo "FAIL: pdftoppm rendered no page"; exit 1; }
'
echo "OK: pdftoppm renders PDF pages as uid 1000"
