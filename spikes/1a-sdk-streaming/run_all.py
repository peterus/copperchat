"""Run every spike 1A question, then write results/summary.md."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = [
    "q_a_lazy_pull.py",
    "q_b_interrupt.py",
    "q_c_resume_fork.py",
    "q_d_tools_empty_mcp.py",
    "q_ef_usage_ratelimit.py",
    "q_g_can_use_tool.py",
    "q_h_subagent.py",
]


def main() -> int:
    worst = 0
    skipped = False
    for script in SCRIPTS:
        print(f"### {script}", flush=True)
        proc = subprocess.run(
            [sys.executable, script],
            check=False,
            cwd=HERE,
            text=True,
            capture_output=True,
            timeout=900,
        )
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr[-4000:])
        skipped |= proc.stdout.startswith("SKIP:")
        worst = max(worst, proc.returncode)
    if skipped:
        return 0
    lines = ["| Question | Check | Status | Evidence |", "|---|---|---|---|"]
    for f in sorted((HERE / "results").glob("*.json")):
        data = json.loads(f.read_text())
        for row in data["rows"]:
            ev = json.dumps(row["evidence"])[:160].replace("|", "\\|")
            lines.append(f"| {data['question']} | {row['name']} | {row['status']} | `{ev}` |")
    (HERE / "results" / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
