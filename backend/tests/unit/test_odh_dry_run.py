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
    assert f"Sum per active board project: {total / odh.MB:,.0f} MB" in memory
    assert "D5 figure: 4.5-5.0 GB" in memory
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
