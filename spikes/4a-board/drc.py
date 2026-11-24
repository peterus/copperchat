"""DRC through the API task (StartDrcTask -> GetTaskStatus -> GetDrcTaskResult) and via kicad-cli."""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

from kipy.proto.board import board_rules_pb2, board_tasks_pb2
from kipy.proto.common import tasks_pb2

from harness import cli


def api_drc(board, *, parity: bool = False, zones_filled: bool = False, timeout_s: float = 300) -> tuple[list, float]:
    t0 = time.monotonic()
    start = board_tasks_pb2.StartDrcTask(board=board.document, report_all_track_errors=True,
                                         test_schematic_parity=parity, require_zones_filled=zones_filled)
    tok = board.client.send(start, tasks_pb2.StartTaskResponse).token
    while time.monotonic() - t0 < timeout_s:
        st = board.client.send(tasks_pb2.GetTaskStatus(token=tok), tasks_pb2.TaskStatusResponse)
        if st.state != tasks_pb2.TS_RUNNING:
            break
        time.sleep(0.1)
    if st.state != tasks_pb2.TS_COMPLETED:
        raise RuntimeError(f"DRC task {tasks_pb2.TaskState.Name(st.state)}: {st.error_message}")
    res = board.client.send(board_tasks_pb2.GetDrcTaskResult(token=tok), board_tasks_pb2.DrcTaskResult)
    return list(res.violations), time.monotonic() - t0


def api_counts(violations) -> Counter:
    return Counter(board_rules_pb2.DrcErrorType.Name(v.error_type) for v in violations)


def cli_drc(pcb: Path, out: Path, *, parity: bool = False) -> tuple[dict, float]:
    t0 = time.monotonic()
    args = ["pcb", "drc", "--format", "json", "--severity-all", "-o", str(out)]
    if parity:
        args.append("--schematic-parity")
    cli(*args, str(pcb), timeout=900)
    secs = time.monotonic() - t0
    return json.loads(out.read_text()), secs


def cli_counts(d: dict) -> Counter:
    c = Counter(v["type"] for v in d.get("violations", []))
    c["unconnected_items"] = len(d.get("unconnected_items", []))
    if d.get("schematic_parity"):
        c["schematic_parity"] = len(d["schematic_parity"])
    return c


_ = board_rules_pb2  # DrcMarker lives here
