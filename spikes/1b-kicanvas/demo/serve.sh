#!/usr/bin/env bash
# Serve the demo as static files (GET/HEAD only, no write endpoints) on all interfaces.
# Run ./build.sh first. Detached:  setsid nohup ./serve.sh >/tmp/kicanvas-demo.log 2>&1 &
set -euo pipefail
DEMO="$(cd "$(dirname "$0")" && pwd)"
PORT="${DEMO_PORT:-8765}"  # not $PORT: hosts often export it for other services
for f in vendor/kicanvas.js projects.json fonts/material-symbols-outlined.woff2; do
  [ -e "$DEMO/$f" ] || { echo "missing $f, run ./build.sh first" >&2; exit 1; }
done
echo $$ > "${PIDFILE:-/tmp/kicanvas-demo.pid}"
exec python3 -m http.server "$PORT" --bind 0.0.0.0 --directory "$DEMO"
