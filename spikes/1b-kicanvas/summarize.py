"""Build <results dir>/summary.md from results.jsonl (default: results/)."""

import json
import sys
from pathlib import Path

d = Path(sys.argv[1] if len(sys.argv) > 1 else "results")
rows = [json.loads(line) for line in (d / "results.jsonl").read_text().splitlines()]
out = ["| Question | Check | Status | Evidence |", "|---|---|---|---|"]
for r in rows:
    ev = json.dumps(r["evidence"])[:160].replace("|", "\\|")
    out.append(f"| {r['question']} | {r['check']} | {r['status']} | `{ev}` |")
(d / "summary.md").write_text("\n".join(out) + "\n")
print("\n".join(out))
