"""Runs all spike questions; writes results/summary.md."""

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
QUESTIONS = [
    "q01_cli",
    "q02_connect",
    "q03_get_items",
    "q04_save",
    "q05_export",
    "q06_erc_drc",
    "q07_lifecycle",
]


def main() -> int:
    rows, worst = [], 0
    for q in QUESTIONS:
        res = HERE / "results" / f"{q}.json"
        res.unlink(missing_ok=True)  # never report a previous run's result
        r = subprocess.run(
            [sys.executable, f"{q}.py"],
            check=False,
            cwd=HERE,
            capture_output=True,
            text=True,
            timeout=900,
        )
        print(r.stdout, end="")
        print(r.stderr, end="", file=sys.stderr)
        if r.stdout.startswith("SKIP:"):
            rows.append(f"| {q} | SKIP | |")
            continue
        worst = max(worst, r.returncode)
        if not res.exists():
            worst = max(worst, 1)
            rows.append(f"| {q} | CRASH (rc={r.returncode}, no results file) | |")
            continue
        checks = json.loads(res.read_text())["checks"]
        fails = [c["name"] for c in checks if not c["ok"]]
        rows.append(
            f"| {q} | {len(checks) - len(fails)} pass / {len(fails)} fail | {', '.join(fails)} |"
        )
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / "summary.md").write_text(
        "| Question | Result | Failed checks |\n|---|---|---|\n" + "\n".join(rows) + "\n"
    )
    return 0 if all("SKIP" in r for r in rows) else worst


if __name__ == "__main__":
    raise SystemExit(main())
