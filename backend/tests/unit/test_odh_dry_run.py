"""`scripts/odh-dry-run.py` (SP4 P9a Task 3): the pure parts — the output-dir guard, the
checkout copy, the answers schema, the report's memory section and the PID sampler."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

from tests.kicad_e2e.flow import FlowResult, StepLog

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "odh-dry-run.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("odh_dry_run", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("odh_dry_run", mod)
    spec.loader.exec_module(mod)
    return mod


odh = _load()


def test_refuses_out_inside_repo(tmp_path: Path) -> None:
    with pytest.raises(odh.UsageError, match="inside the repository"):
        odh.refuse_out_inside_repo(odh.REPO / "backend" / "dry-run-out")
    with pytest.raises(odh.UsageError):
        odh.refuse_out_inside_repo(odh.REPO)
    assert odh.refuse_out_inside_repo(tmp_path / "out") == (tmp_path / "out").resolve()
    # the CLI refuses before anything is copied or started
    rc = odh.main(
        [
            "--odh",
            str(tmp_path),
            "--project",
            "x.kicad_pro",
            "--answers",
            "a.json",
            "--out",
            str(odh.REPO / "out"),
        ]
    )
    assert rc == 2


def test_copy_skips_git(tmp_path: Path) -> None:
    src = tmp_path / "checkout"
    (src / ".git" / "objects").mkdir(parents=True)
    (src / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    board = src / "hardware" / "main"
    (board / ".kicad-agent").mkdir(parents=True)
    (board / ".kicad-agent" / "state.json").write_text("{}")
    (board / "main.kicad_pro").write_text("{}")
    (board / "~main.kicad_pro.lck").write_text("")
    (src / "footprints" / "Lib.pretty").mkdir(parents=True)
    (src / "footprints" / "Lib.pretty" / "X.kicad_mod").write_text("(footprint X)")
    copy = odh.copy_checkout(src)
    try:
        assert copy.name == "checkout" and copy.parent.name.startswith("odh-dry-run-")
        assert not (copy / ".git").exists()
        assert not (copy / "hardware" / "main" / ".kicad-agent").exists()
        assert not (copy / "hardware" / "main" / "~main.kicad_pro.lck").exists()
        assert (copy / "hardware" / "main" / "main.kicad_pro").is_file()
        assert (copy / "footprints" / "Lib.pretty" / "X.kicad_mod").is_file()
        assert (src / ".git" / "HEAD").is_file()  # the original is untouched
    finally:
        import shutil

        shutil.rmtree(copy.parent)


def test_answers_schema() -> None:
    with pytest.raises(odh.UsageError, match="'outline'"):
        odh.load_answers({"rules": {"layers": 4}})
    with pytest.raises(odh.UsageError, match="'rules'"):
        odh.load_answers({"outline": {}})
    with pytest.raises(odh.UsageError, match="'colour'"):
        odh.load_answers({"outline": {}, "rules": {}, "colour": "red"})
    with pytest.raises(odh.UsageError, match="'blocks'"):
        odh.load_answers({"outline": {}, "rules": {}, "blocks": [1]})
    a = odh.load_answers(
        {"outline": {"shape": "rect"}, "rules": {"layers": 4}, "blocks": None, "fanout": ["GND"]}
    )
    assert a.blocks is None and a.zones is None and a.stitch == {} and a.fanout == ("GND",)


def _fake_result() -> FlowResult:
    return FlowResult(
        steps=[StepLog(1, "run-flow-1", "pcb_sync_from_schematic", 9.5, 3, {"unplaced": 26})],
        metrics={
            "hard_failures": 0,
            "reviews": 1,
            "verdict": "review",
            "metrics": [
                {"id": "M2", "value": 0, "verdict": "pass", "offenders": []},
                {"id": "M9", "value": 1, "verdict": "review", "offenders": [{"what": "sw"}]},
            ],
        },
        seconds=123.0,
        renders=[],
        fab_files=["bom-jlcpcb.csv"],
        hashes={},
        fill={"completion": 1.0, "connections": {"routed": 5, "total": 5}, "open": []},
    )


def test_report_has_memory_section() -> None:
    gb = 1024 * odh.MB
    mem = odh.Memory(
        api_server=2 * gb,
        api_before_fp=int(1.5 * gb),
        api_after_fp=2 * gb,
        jvm=600 * odh.MB,
        cpu_worker=400 * odh.MB,
        cpu_workers=[400 * odh.MB],
        raster=80 * odh.MB,
        cli={"svg export": 300 * odh.MB, "3d render": 700 * odh.MB, "parity drc": 500 * odh.MB},
        concurrent_mb=5300.0,
        concurrent_step="turn 9 pcb_check",
        concurrent_parts={"api-server": 2900.0, "kicad-cli parity drc": 2400.0},
    )
    text = odh.build_report(_fake_result(), mem, project="hw/main.kicad_pro", seconds=123.0)
    for heading in ("## Metrics", "## Memory", "## Runtime"):
        assert heading in text
    memory = text.split("## Memory", 1)[1]
    parts = [line for line in memory.splitlines() if line.startswith("- ")]
    assert len(parts) == 5, parts
    for label in (
        "api-server RSS",
        "JVM RSS",
        "CPU worker VmHWM",
        "raster worker peak",
        "kicad-cli peak",
    ):
        assert sum(label in p for p in parts) == 1, (label, parts)
    details = [line for line in memory.splitlines() if line.startswith("  - ")]
    assert [d.split(":")[0].strip("- ") for d in details] == [
        "3d render",
        "parity drc",
        "svg export",
    ]
    assert "kicad-cli peak: 700 MB" in memory
    total = 2 * gb + 600 * odh.MB + 400 * odh.MB + 80 * odh.MB + 700 * odh.MB
    assert mem.total == total
    assert f"Sum of peaks (upper bound): {total / odh.MB:,.0f} MB" in memory
    assert (
        "Concurrent peak (lower bound, 0.5 s samples): 5,300 MB (5.18 GB)** in step "
        "turn 9 pcb_check; shares: api-server 2,900 MB, kicad-cli parity drc 2,400 MB"
    ) in memory
    assert "D5 figure: 4.5-5.0 GB" in memory
    assert "concurrent peak ABOVE it, sum of peaks within it" in memory  # 5.18 vs 3.75 GB
    assert "| M9 | 1 | review | sw |" in text


def test_jvm_sampler_by_pid() -> None:
    """The sampler reads the peak VmRSS of a process by its PID (a child allocating
    200 MB, then sleeping), independent of RUSAGE_CHILDREN."""
    code = "b = bytearray(200 * 1024 * 1024)\nfor i in range(0, len(b), 4096): b[i] = 1\n"
    code += "import time; time.sleep(30)"
    child = subprocess.Popen([sys.executable, "-c", code])
    try:
        sampler = odh.PeakSampler(lambda: [child.pid], lambda pid: "jvm", interval=0.1).start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and sampler.peak("jvm") < 200 * odh.MB:
            time.sleep(0.1)
        sampler.stop()
        assert sampler.peak("jvm") >= 200 * odh.MB, sampler.peak_rss
        assert sampler.hwm[child.pid] >= 200 * odh.MB
    finally:
        child.kill()
        child.wait()


def test_kind_of_classifies_processes() -> None:
    cli = "/usr/bin/kicad-cli"
    assert odh.kind_of([cli, "api-server", "--socket", "s", "p.kicad_pro"], cli) == "api-server"
    assert odh.kind_of([cli, "pcb", "export", "svg", "-o", "x"], cli) == "kicad-cli svg export"
    assert odh.kind_of([cli, "pcb", "render", "--side", "top"], cli) == "kicad-cli 3d render"
    assert odh.kind_of([cli, "pcb", "drc", "--schematic-parity"], cli) == "kicad-cli parity drc"
    assert odh.kind_of(["/jre/bin/java", "-Xmx1g", "-jar", "/fr/freerouting-2.5.0.jar"], cli) == (
        "jvm"
    )
    assert odh.kind_of(["python3", "/app/copperchat/views/_raster_worker.py", "a"], cli) == "raster"
    spawn = ["python3", "-c", "from multiprocessing.spawn import spawn_main; spawn_main()"]
    assert odh.kind_of(spawn, cli) == "cpu worker"
    tracker = ["python3", "-c", "from multiprocessing.resource_tracker import main;main(5)"]
    assert odh.kind_of(tracker, cli) is None
    assert odh.kind_of(["bash"], cli) is None


def test_svg_stats_and_renders_section(tmp_path: Path) -> None:
    svg = tmp_path / "a.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><g><path d="M0 0"/></g></svg>')
    assert odh.svg_stats(svg) == {"bytes": svg.stat().st_size, "elements": 3}
    assert odh.svg_stats(tmp_path / "missing.svg") == {}
    text = odh.build_report(
        _fake_result(), odh.Memory(), project="p", renders={"01-a.png": odh.svg_stats(svg)}
    )
    assert "## Renders" in text and "| 01-a.png |" in text


def test_report_lists_the_after_failure_calls() -> None:
    rows = [
        {"tool": "pcb_check", "args": {"level": "full"}, "seconds": 9.1, "ok": True,
         "metrics": {"hard_failures": 2, "verdict": "fail", "metrics": [
             {"id": "M2", "value": 5, "verdict": "fail", "offenders": [{"what": "open"}]}]}},
        {"tool": "pcb_export_3d", "args": {"format": "glb"}, "seconds": 1.0, "ok": False,
         "error": {"error": {"what": "x"}}},
    ]  # fmt: skip
    text = odh.build_report(None, odh.Memory(), project="p", error="boom", after_failure=rows)
    part = text.split("## After the failure (measurement only)", 1)[1]
    assert '- pcb_check {"level": "full"}: 9.1 s, ok' in part
    assert "pcb_export_3d" in part and "error" in part
    assert "hard failures 2" in part and "| M2 | 5 | fail | open |" in part


def test_copy_follows_symlinks_out_of_the_checkout(tmp_path: Path) -> None:
    """A symlink in the checkout is copied as its target: writes to the copy never reach
    the original through it."""
    import shutil

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "lib.kicad_sym").write_text("orig")
    src = tmp_path / "checkout"
    src.mkdir()
    (src / "lib").symlink_to(outside)
    copy = odh.copy_checkout(src)
    try:
        assert not (copy / "lib").is_symlink()
        (copy / "lib" / "lib.kicad_sym").write_text("changed")
        assert (outside / "lib.kicad_sym").read_text() == "orig"
    finally:
        shutil.rmtree(copy.parent)


def test_sampler_reclassifies_until_exec_and_uses_hwm() -> None:
    """A process first seen before its exec (kind None) is classified again on the next
    sample; a kind's peak keeps VmHWM (a spike between two samples)."""
    import os

    kinds = iter([None, "jvm", "other"])
    s = odh.PeakSampler(lambda: [os.getpid()], lambda pid: next(kinds))
    s.sample()
    assert s.kinds[os.getpid()] is None
    s.sample()
    assert s.kinds[os.getpid()] == "jvm"
    s.sample()
    assert s.kinds[os.getpid()] == "jvm"  # classified once it has a kind
    s.peak_rss[os.getpid()] = 1
    assert s.peak("jvm") == s.hwm[os.getpid()] > 1


def test_answer_fields_match_flow_answers() -> None:
    """Every `flow.Answers` field can be given in the answers JSON (and nothing else)."""
    import dataclasses

    from tests.kicad_e2e.flow import Answers

    assert set(odh.ANSWER_FIELDS) == {f.name for f in dataclasses.fields(Answers)}
    a = odh.load_answers({"outline": {}, "rules": {}, "fill_retry": {"timeout_s": 600}})
    assert a.fill_retry == {"timeout_s": 600}


def test_concurrent_peak() -> None:
    """The sample with the largest Σ VmRSS, its step and each part's share (MB)."""
    samples = [
        ("turn 1 pcb_route", {"api-server": 3000 * odh.MB, "cpu worker": 900 * odh.MB}),
        ("turn 2 pcb_check", {"api-server": 3100 * odh.MB, "kicad-cli parity drc": 2600 * odh.MB}),
        ("turn 3 pcb_fill", {"api-server": 3100 * odh.MB, "jvm": 1500 * odh.MB}),
    ]
    total, step, shares = odh.concurrent_peak(samples)
    assert total == 5700.0
    assert step == "turn 2 pcb_check"
    assert shares == {"api-server": 3100.0, "kicad-cli parity drc": 2600.0}
    assert odh.concurrent_peak([]) == (0.0, "", {})


def test_sampler_records_concurrent_samples_by_step() -> None:
    """Each sample sums the classified processes by kind; samples belong to the step that
    ended next (the flow's on_step marks), later ones to "after the flow"."""
    import os

    s = odh.PeakSampler(lambda: [os.getpid(), os.getpid() + 10**7], lambda pid: "api-server")
    s.sample()
    s.mark("turn 1 pcb_route")
    s.sample()
    labelled = s.labelled()
    assert [step for step, _ in labelled] == ["turn 1 pcb_route", "after the flow"]
    assert set(labelled[0][1]) == {"api-server"} and labelled[0][1]["api-server"] > 0
    assert odh.step_at(0.0, []) == "after the flow"
