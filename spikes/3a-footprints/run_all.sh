#!/usr/bin/env bash
# Runs every question in the pinned kicad-base image; summary in results/summary.md
set -uo pipefail
cd "$(dirname "$0")"
mkdir -p results
rm -f results/*.json results/*.log results/*.svg results/summary.md
rm -rf results/copperchat.pretty
echo "| Question | Result | Failed checks |" > results/summary.md
echo "|---|---|---|" >> results/summary.md
rc=0
for q in q01_create q02_library q03_shared q04_coexist q05_read q06_kipy q07_assign q08_render; do
  timeout 600 ./run.sh "$q.py" > "results/$q.log" 2>&1
  code=$?
  if [ ! -f "results/$q.json" ]; then
    echo "| $q | CRASH (exit $code, see results/$q.log) | |" >> results/summary.md
    rc=1
    continue
  fi
  python3 - "$q" >> results/summary.md <<'P'
import json, sys
d = json.load(open(f"results/{sys.argv[1]}.json"))
bad = [c["name"] for c in d["checks"] if not c["ok"]]
print(f"| {sys.argv[1]} | {len(d['checks']) - len(bad)} pass / {len(bad)} fail | {', '.join(bad)} |")
P
  [ "$code" -ne 0 ] && rc=1
done
cat results/summary.md
exit $rc
