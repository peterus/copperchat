#!/usr/bin/env bash
# Runs every question in the pinned kicad-base image; summary in results/summary.md.
# Prerequisites (host): docker, ImageMagick `convert`, ./fetch_odh.sh and ./fetch_deps.sh done once.
set -uo pipefail
cd "$(dirname "$0")"
mkdir -p results
rm -f results/*.json results/*.log results/*.png results/*.svg results/*.csv results/*.zip results/summary.md
echo "| Question | Result | Failed checks | Seconds |" > results/summary.md
echo "|---|---|---|---|" >> results/summary.md
rc=0
for q in q01_netlist q02_setup q03_place q04_route q04c_astar q05_zones q06_metrics q07_show q08_fab; do
  t0=$(date +%s)
  timeout -k 30 1800 ./run.sh "$q.py" > "results/$q.log" 2>&1
  code=$?
  secs=$(( $(date +%s) - t0 ))
  if [ ! -f "results/$q.json" ]; then
    echo "| $q | CRASH (exit $code, see results/$q.log) | | $secs |" >> results/summary.md
    rc=1
    continue
  fi
  python3 - "$q" "$secs" >> results/summary.md <<'P'
import json, sys
d = json.load(open(f"results/{sys.argv[1]}.json"))
bad = [c["name"] for c in d["checks"] if not c["ok"]]
print(f"| {sys.argv[1]} | {len(d['checks']) - len(bad)} pass / {len(bad)} fail | {', '.join(bad)} | {sys.argv[2]} |")
P
  [ "$code" -ne 0 ] && rc=1
done
cat results/summary.md
exit $rc
