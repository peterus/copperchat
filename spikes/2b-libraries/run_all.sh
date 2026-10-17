#!/usr/bin/env bash
# Runs every question in the pinned kicad-base image; summary in results/summary.md
set -uo pipefail
cd "$(dirname "$0")"
rm -rf results; mkdir -p results
: > results/summary.md
echo "| Question | Result | Failed checks |" >> results/summary.md
echo "|---|---|---|" >> results/summary.md
for q in q01_load q02_symbols q03_footprints q04_project_libs q05_index; do
  ./run.sh "$q.py" > "results/$q.log" 2>&1
  python3 - "$q" >> results/summary.md <<'P'
import json, sys
d = json.load(open(f"results/{sys.argv[1]}.json"))
bad = [c["name"] for c in d["checks"] if not c["ok"]]
print(f"| {sys.argv[1]} | {len(d['checks']) - len(bad)} pass / {len(bad)} fail | {', '.join(bad)} |")
P
done
cat results/summary.md
