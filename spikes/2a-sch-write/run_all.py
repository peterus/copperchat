"""Runs all spike questions (inside the kicad-base image: ./run.sh python run_all.py);
writes results/summary.md. Q12 (KiCanvas) runs on the host afterwards:
cd kicanvas && npm ci --include=dev && npx playwright test --reporter=list; then
python3 run_all.py --summary-only adds it to the summary.

Exit code: 0 when every failed check is one of the documented findings (EXPECTED_FAIL,
explained in docs/spikes/2a-sch-write.md), 1 on any other failure, crash or timeout.
Q12 counts towards the exit code only with --summary-only (run after the host check)."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
TIMEOUT_S = 1200
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
# Failed checks that are findings about KiCad/kipy behaviour (each has a doc entry).
EXPECTED_FAIL = {
    "q01_place": {"rotate_then_mirror_matrix_as_requested"},
    "q02_fields": {
        "multi_unit_full_definition_update_responds",
        "multi_unit_full_definition_update_one_call_responds",
        "multi_unit_project_lib_full_definition_update_responds",
        "multi_unit_fields_synced_by_kicad",
    },
    "q03_connect": {
        "netlist_job_reflects_unsaved_edits",
        "netlist_job_reflects_edits_after_save",
        "png_job_reflects_edits_after_save",
        "revert_refreshes_export_jobs",
    },
    "q04_transform": {"move_symbol_moves_fields", "move_symbol_drags_attached_wires", "subsheet_update_via_unbound_schematic_applies"},
    "q05_delete": {
        "dangling_junction_kept_after_push",
        "delete_wire_keeps_junction",
        "delete_wire_keeps_neighbour_segments",
        "collinear_pair_kept_separate_on_create",
    },
    "q06_commit": {"update_of_missing_item_raises", "create_with_existing_uuid_raises", "recreate_with_sibling_keeps_library_link"},
    "q07_sheets": {"relative_sheetfile_new_sheet_save", "workaround_field_fix_inside_create_commit"},
    "q08_save_latency": {
        "new_symbol_on_shared_sheet_visible_in_other_instance",
        "copy_without_save_finds_all_hierarchy_files",
        "copy_without_save_has_subsheet_edits",
        "shared_sheet_second_instance_unique_ref_after_save",
    },
    "q09_bbox": {"api_bbox_matches_plot_within_0_6mm"},
    "q10_duplicate": {"definition_keeps_all_body_style_pins", "dup_symbols_create_items_keep_library_link"},
    "q12_kicanvas": {"kicanvas_loads_q10_duplicate_B"},
}


def row(q: str, res: Path, label: str | None = None) -> tuple[str, int]:
    checks = json.loads(res.read_text())["checks"]
    fails = [c["name"] for c in checks if not c["ok"]]
    unexpected = [f for f in fails if f not in EXPECTED_FAIL.get(q, set())]
    shown = [f"**{f}** (unexpected)" if f in unexpected else f for f in fails]
    line = f"| {label or q} | {len(checks) - len(fails)} pass / {len(fails)} fail | {', '.join(shown)} |"
    return line, 1 if unexpected else 0


def run_question(q: str) -> tuple[subprocess.CompletedProcess | None, str]:
    """Runs one question in its own process group, so a timeout also kills the
    api-server it started (Server.__exit__ does not run on SIGKILL)."""
    p = subprocess.Popen(
        [sys.executable, f"{q}.py"], cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
    )
    try:
        out, err = p.communicate(timeout=TIMEOUT_S)
        return subprocess.CompletedProcess(p.args, p.returncode, out, err), ""
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        out, err = p.communicate()
        print(out, end="")
        print(err, end="", file=sys.stderr)
        return None, f"TIMEOUT after {TIMEOUT_S}s"


def main() -> int:
    summary_only = "--summary-only" in sys.argv  # rebuild summary.md from results/*.json
    rows, worst = [], 0
    for q in QUESTIONS:
        res = HERE / "results" / f"{q}.json"
        if summary_only:
            line, rc = row(q, res) if res.exists() else (f"| {q} | missing | |", 1)
            rows.append(line)
            worst = max(worst, rc)
            continue
        res.unlink(missing_ok=True)  # never report a previous run's result
        r, problem = run_question(q)
        if r is None:
            worst = 1
            rows.append(f"| {q} | {problem} | |")
            continue
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
        worst = max(worst, rc)
    kc = HERE / "results" / "q12_kicanvas.json"
    if kc.exists():
        # written by the separate host run; it may predate this run's results/files
        line, rc = row("q12_kicanvas", kc, label="q12_kicanvas (host, separate run)")
        rows.append(line)
        if summary_only:  # the host run just wrote it; a docker run may only see a stale copy
            worst = max(worst, rc)
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / "summary.md").write_text("| Question | Result | Failed checks |\n|---|---|---|\n" + "\n".join(rows) + "\n")
    return 0 if all("SKIP" in r for r in rows) else worst


if __name__ == "__main__":
    raise SystemExit(main())
