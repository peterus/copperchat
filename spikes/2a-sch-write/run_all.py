"""Runs all spike questions (inside the kicad-base image: ./run.sh python run_all.py);
writes results/summary.md. Q12 (KiCanvas) runs on the host afterwards:
cd kicanvas && npm ci --include=dev && npx playwright test --reporter=list; then
python3 run_all.py --summary-only adds it to the summary."""

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
QUESTIONS = [
    "q01_place",
    "q02_fields",
    "q03_connect",
    "q04_transform",
    "q05_delete",
    "q06_commit",
    "q07_sheets",
    "q08_save_latency",
    "q09_bbox",
    "q10_duplicate",
    "q11_files_load",
]


def row(q: str, res: Path) -> tuple[str, int]:
    checks = json.loads(res.read_text())["checks"]
    fails = [c["name"] for c in checks if not c["ok"]]
    return f"| {q} | {len(checks) - len(fails)} pass / {len(fails)} fail | {', '.join(fails)} |", 1 if fails else 0


def main() -> int:
    summary_only = "--summary-only" in sys.argv  # rebuild summary.md from results/*.json
    rows, worst = [], 0
    for q in QUESTIONS:
        res = HERE / "results" / f"{q}.json"
        if summary_only:
            rows.append(row(q, res)[0] if res.exists() else f"| {q} | missing | |")
            continue
        res.unlink(missing_ok=True)  # never report a previous run's result
        r = subprocess.run([sys.executable, f"{q}.py"], check=False, cwd=HERE, capture_output=True, text=True, timeout=1200)
        print(r.stdout, end="")
        print(r.stderr, end="", file=sys.stderr)
        if r.stdout.startswith("SKIP:"):
            rows.append(f"| {q} | SKIP | |")
            continue
        if not res.exists():
            worst = 1
            rows.append(f"| {q} | CRASH (rc={r.returncode}, no results file) | |")
            continue
        line, rc = row(q, res)
        rows.append(line)
        worst = max(worst, rc, r.returncode)
    kc = HERE / "results" / "q12_kicanvas.json"
    if kc.exists():
        rows.append(row("q12_kicanvas (host)", kc)[0])
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / "summary.md").write_text(
        "| Question | Result | Failed checks |\n|---|---|---|\n" + "\n".join(rows) + "\n"
    )
    return 0 if all("SKIP" in r for r in rows) else worst


if __name__ == "__main__":
    raise SystemExit(main())
