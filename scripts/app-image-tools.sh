#!/usr/bin/env bash
# scripts/app-image-tools.sh IMAGE — external binaries the agent needs at runtime are on PATH
# for the app user: pdftoppm (the Claude CLI's Read tool renders PDF pages with it), and
# CairoSVG with the base image's libcairo2 (fp_render rasterises footprints with it).
set -euo pipefail
image="${1:?usage: app-image-tools.sh IMAGE}"
timeout 120 docker run --rm --user 1000:1000 --entrypoint bash "$image" -euc '
  command -v pdftoppm || { echo "FAIL: pdftoppm not on PATH"; exit 1; }
  pdftoppm -v
  cd "$(mktemp -d)"
  python -c "from PIL import Image; Image.new(\"RGB\", (200, 100), \"white\").save(\"sample.pdf\")"
  pdftoppm -png -f 1 -l 1 sample.pdf page
  ls page*.png >/dev/null || { echo "FAIL: pdftoppm rendered no page"; exit 1; }
  python - <<"PY" || { echo "FAIL: cairosvg / libcairo2 cannot rasterise"; exit 1; }
import io
import cairosvg.surface
from PIL import Image
assert tuple(int(p) for p in cairosvg.__version__.split(".")[:2]) >= (2, 7), cairosvg.__version__
def refuse(url, resource_type):
    raise ValueError("external resource refused")
svg = b"<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"1mm\" height=\"1mm\" viewBox=\"0 0 1 1\"><rect width=\"1\" height=\"1\"/></svg>"
png = cairosvg.surface.PNGSurface.convert(bytestring=svg, url_fetcher=refuse, unsafe=False)
assert Image.open(io.BytesIO(png)).format == "PNG"
print("cairosvg", cairosvg.__version__, "ok")
PY
'
echo "OK: pdftoppm renders PDF pages and cairosvg rasterises SVG as uid 1000"
