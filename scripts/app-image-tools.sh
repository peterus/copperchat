#!/usr/bin/env bash
# scripts/app-image-tools.sh IMAGE — external binaries the agent needs at runtime are on PATH
# for the app user: pdftoppm (the Claude CLI's Read tool renders PDF pages with it), and
# CairoSVG with the base image's libcairo2 (fp_render rasterises footprints with it, in
# its rasteriser process), the Freerouting runtime (Java 25 JRE + JAR, notices) and prlimit
# (the Freerouting runner sets its resource limits with it).
set -euo pipefail
image="${1:?usage: app-image-tools.sh IMAGE}"
timeout 120 docker run --rm --user 1000:1000 --entrypoint bash "$image" -euc '
  command -v pdftoppm || { echo "FAIL: pdftoppm not on PATH"; exit 1; }
  /opt/freerouting/jre/bin/java -version 2>&1 | grep -q "version \"25" \
    || { echo "FAIL: /opt/freerouting/jre/bin/java is not Java 25"; exit 1; }
  test -r /opt/freerouting/freerouting-2.5.0.jar || { echo "FAIL: Freerouting JAR not readable"; exit 1; }
  test -r /usr/share/doc/copperchat/LICENSES/freerouting.md \
    || { echo "FAIL: LICENSES/freerouting.md missing"; exit 1; }
  command -v prlimit || { echo "FAIL: prlimit not on PATH"; exit 1; }
  pdftoppm -v
  cd "$(mktemp -d)"
  python -c "from PIL import Image; Image.new(\"RGB\", (200, 100), \"white\").save(\"sample.pdf\")"
  pdftoppm -png -f 1 -l 1 sample.pdf page
  ls page*.png >/dev/null || { echo "FAIL: pdftoppm rendered no page"; exit 1; }
  python - <<"PY" || { echo "FAIL: cairosvg / libcairo2 cannot rasterise"; exit 1; }
import asyncio
import io
import cairosvg
from PIL import Image
from copperchat.views.footprint_render import rasterise
assert tuple(int(p) for p in cairosvg.__version__.split(".")[:2]) >= (2, 7), cairosvg.__version__
svg = b"<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"1mm\" height=\"1mm\" viewBox=\"0 0 1 1\"><rect width=\"1\" height=\"1\"/></svg>"
# the way fp_render runs it: the rasteriser process (python -I -m copperchat.views._raster_worker)
png, _scale = asyncio.run(rasterise(svg, (1.0, 1.0)))
assert Image.open(io.BytesIO(png)).format == "PNG"
print("cairosvg", cairosvg.__version__, "ok (rasteriser process)")
PY
'
echo "OK: pdftoppm renders PDF pages, cairosvg rasterises SVG, Java 25 + Freerouting JAR + prlimit present as uid 1000"
