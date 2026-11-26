#!/usr/bin/env python3
"""Manual dry run of the whole board flow on a real project (SP4 P9a, spec §18.3, §20).

    cd backend && uv run python ../scripts/odh-dry-run.py --odh <checkout> \\
        --project <relative path of the board's .kicad_pro> --answers <answers.json> [--out <dir>]

Run inside the kicad-base image with Freerouting fetched (`COPPERCHAT_FREEROUTING_HOME`).
Not part of CI. The checkout is copied (without `.git` and CopperChat's `.kicad-agent`
state) into a fresh temp dir and only the copy is opened; the original is never written.
`--out` must lie outside this repository: nothing of the project is ever committed.

The flow is `tests/kicad_e2e/flow.run_flow` (the same turns as the sp4_synth golden) with
the user's answers from the JSON file (`Answers` fields; `blocks: null` = `pcb_analyze`'s
proposal). Output in `--out` (default a fresh `$TMPDIR/odh-dry-run-<UTC date>-*`): `report.md` (per
step: tool, seconds, cost, result; final metrics with verdicts; fill completion; open
connections; fab files; runtime; memory), the renders, `metrics.json`.

Memory (owner decision D5, "≈ 4.5-5 GB per active board project"), five parts:
1. api-server: peak `VmHWM` of `/proc/<pid>/status` (also before / after
   `activate_footprints()`, the footprint libraries a board session loads for fp tools);
2. Freerouting JVM: max `VmRSS` sampled every 0.5 s by PID (RSS, not the heap setting);
3. CPU worker (`copperchat.board.worker`): its `VmHWM` (each replaced worker's last value);
4. raster worker (`_raster_worker.py`): max `VmRSS` sampled by PID;
5. kicad-cli (one at a time under `BOARD_CLI_LOCK`): max `VmRSS` per kind (svg export,
   3d render, parity drc, gerbers, drill, pos, bom, step, glb, dsn), the largest counts.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))  # tests.kicad_e2e.flow (the flow of the e2e golden)

D5_GB = (4.5, 5.0)  # owner decision D5: memory per active board project
SAMPLE_S = 0.5
COPY_IGNORE = (".git", ".kicad-agent", "~*.lck")  # VCS, CopperChat's own state, locks
MB = 1024 * 1024


class UsageError(Exception):
    """A bad command line or answers file (exit code 2)."""


# ---- inputs -------------------------------------------------------------------------------


def refuse_out_inside_repo(out: Path, repo: Path = REPO) -> Path:
    """`out` resolved; refused when it lies inside this repository (nothing of the
    project may end up in a commit)."""
    resolved = out.expanduser().resolve()
    if resolved == repo.resolve() or resolved.is_relative_to(repo.resolve()):
        raise UsageError(f"--out {out} lies inside the repository {repo}: choose a dir outside")
    return resolved


def copy_checkout(src: Path) -> Path:
    """A copy of the checkout `src` in a fresh temp dir (`odh-dry-run-*`), without `.git`,
    `.kicad-agent` and lock files; repo-relative paths (footprint libraries) are kept.
    Returns the copy's root (`<tmp>/<src name>`)."""
    if not src.is_dir():
        raise UsageError(f"--odh {src} is not a directory")
    tmp = Path(tempfile.mkdtemp(prefix="odh-dry-run-"))
    dst = tmp / src.resolve().name
    # symlinks are copied as their targets: a link into the original checkout must never
    # let the flow write through to it
    shutil.copytree(
        src,
        dst,
        symlinks=False,
        ignore=shutil.ignore_patterns(*COPY_IGNORE),
        ignore_dangling_symlinks=True,
    )
    return dst


ANSWER_FIELDS = {
    "outline": dict,
    "rules": dict,
    "blocks": (dict, type(None)),
    "zones": (dict, type(None)),
    "stitch": dict,
    "keep_channels": (int, float),
    "fanout": list,
    "accept_warnings": dict,
}
REQUIRED_ANSWERS = ("outline", "rules")


def load_answers(data: Mapping[str, Any]) -> Any:
    """`flow.Answers` from the JSON object; a missing required field, an unknown field or
    a wrong type is a usage error naming the field."""
    from tests.kicad_e2e.flow import Answers

    if not isinstance(data, Mapping):
        raise UsageError("answers: expected a JSON object")
    for name in REQUIRED_ANSWERS:
        if name not in data:
            raise UsageError(f"answers: missing field {name!r}")
    for name, value in data.items():
        if name not in ANSWER_FIELDS:
            raise UsageError(f"answers: unknown field {name!r}")
        if not isinstance(value, ANSWER_FIELDS[name]) or isinstance(value, bool):
            raise UsageError(f"answers: field {name!r} has the wrong type")
    kw: dict[str, Any] = {"blocks": None, "zones": None, "stitch": {}}
    kw.update(data)
    if "fanout" in kw:
        kw["fanout"] = tuple(kw["fanout"])
    if "keep_channels" in kw:
        kw["keep_channels"] = float(kw["keep_channels"])
    return Answers(**kw)


# ---- memory sampling by PID ---------------------------------------------------------------


def proc_status(pid: int) -> dict[str, int]:
    """`VmRSS` / `VmHWM` (bytes) of `pid` from `/proc/<pid>/status`; {} when gone."""
    out: dict[str, int] = {}
    try:
        text = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return out
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if key in ("VmRSS", "VmHWM"):
            out[key] = int(rest.split()[0]) * 1024
    return out


def proc_cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [a.decode(errors="replace") for a in raw.split(b"\0") if a]


def descendants(root: int) -> list[int]:
    """Every live descendant PID of `root` (from `/proc/*/stat` parent links)."""
    children: dict[int, list[int]] = {}
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            stat = (d / "stat").read_text()
        except OSError:
            continue
        ppid = int(stat.rsplit(")", 1)[1].split()[1])
        children.setdefault(ppid, []).append(int(d.name))
    out: list[int] = []
    todo = [root]
    while todo:
        for c in children.get(todo.pop(), []):
            out.append(c)
            todo.append(c)
    return out


CLI_KINDS = (
    (("pcb", "export", "svg"), "svg export"),
    (("pcb", "render"), "3d render"),
    (("pcb", "drc"), "parity drc"),
    (("pcb", "export", "gerbers"), "gerbers"),
    (("pcb", "export", "drill"), "drill"),
    (("pcb", "export", "pos"), "pos"),
    (("sch", "export", "bom"), "bom"),
    (("pcb", "export", "step"), "step"),
    (("pcb", "export", "glb"), "glb"),
    (("pcb", "export", "specctra"), "dsn"),
    (("sch", "export", "netlist"), "netlist"),
)


def kind_of(argv: list[str], kicad_cli: str) -> str | None:
    """The D5 part a process belongs to, from its argv; None for anything else."""
    if not argv:
        return None
    if "api-server" in argv:
        return "api-server"
    if any("freerouting" in a and a.endswith(".jar") for a in argv):
        return "jvm"
    if any(a.endswith("_raster_worker.py") or "_raster_worker" in a for a in argv):
        return "raster"
    if Path(argv[0]).name == Path(kicad_cli).name:
        words = [a for a in argv[1:] if not a.startswith("-")]
        for prefix, kind in CLI_KINDS:
            if tuple(words[: len(prefix)]) == prefix:
                return f"kicad-cli {kind}"
        return "kicad-cli other"
    joined = " ".join(argv)
    if "multiprocessing" in joined and "resource_tracker" not in joined:
        return "cpu worker"
    return None


@dataclass
class PeakSampler:
    """Samples `VmRSS` / `VmHWM` of the PIDs `pids()` returns every `interval` seconds in
    a thread, by PID (never `RUSAGE_CHILDREN`). `peak_rss[pid]` = max VmRSS seen,
    `hwm[pid]` = the last VmHWM read (a dead process keeps its last value), `kinds[pid]`
    = `classify(pid)` at first sight."""

    pids: Callable[[], Iterable[int]]
    classify: Callable[[int], str | None] = lambda pid: None
    interval: float = SAMPLE_S
    peak_rss: dict[int, int] = field(default_factory=dict)
    hwm: dict[int, int] = field(default_factory=dict)
    kinds: dict[int, str | None] = field(default_factory=dict)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def sample(self) -> None:
        for pid in list(self.pids()):
            st = proc_status(pid)
            if not st:
                continue
            if self.kinds.get(pid) is None:  # again until exec'd (fork → exec)
                self.kinds[pid] = self.classify(pid)
            rss = st.get("VmRSS", 0)
            self.peak_rss[pid] = max(self.peak_rss.get(pid, 0), rss)
            if "VmHWM" in st:
                self.hwm[pid] = st["VmHWM"]

    def _run(self) -> None:
        while not self._stop.is_set():
            self.sample()
            self._stop.wait(self.interval)

    def start(self) -> PeakSampler:
        self._thread = threading.Thread(target=self._run, name="peak-sampler", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.sample()

    def by_kind(self, kind: str) -> list[int]:
        return [p for p, k in self.kinds.items() if k == kind]

    def peak(self, kind: str) -> int:
        """Max over the kind's processes of max(sampled VmRSS, last VmHWM): VmHWM keeps a
        spike between two samples."""
        return max(
            (max(self.peak_rss.get(p, 0), self.hwm.get(p, 0)) for p in self.by_kind(kind)),
            default=0,
        )


@dataclass
class Memory:
    """The D5 breakdown (bytes)."""

    api_server: int = 0
    api_before_fp: int = 0
    api_after_fp: int = 0
    jvm: int = 0
    cpu_worker: int = 0
    cpu_workers: list[int] = field(default_factory=list)  # every worker's VmHWM
    raster: int = 0
    cli: dict[str, int] = field(default_factory=dict)  # kind -> peak VmRSS

    @property
    def cli_peak(self) -> int:
        return max(self.cli.values(), default=0)

    @property
    def total(self) -> int:
        return self.api_server + self.jvm + self.cpu_worker + self.raster + self.cli_peak


def memory_from(sampler: PeakSampler, api: Mapping[str, int]) -> Memory:
    """`Memory` from the sampler and the api-server's VmHWM readings (`end`, `before_fp`,
    `after_fp`)."""
    cli = {
        k.removeprefix("kicad-cli "): sampler.peak(k)
        for k in sorted({k for k in sampler.kinds.values() if k and k.startswith("kicad-cli")})
    }
    workers = [
        sampler.hwm.get(p, sampler.peak_rss.get(p, 0)) for p in sampler.by_kind("cpu worker")
    ]
    api_seen = max((sampler.hwm.get(p, 0) for p in sampler.by_kind("api-server")), default=0)
    return Memory(
        api_server=max(api.get("end", 0), api.get("after_fp", 0), api_seen),
        api_before_fp=api.get("before_fp", 0),
        api_after_fp=api.get("after_fp", 0),
        jvm=sampler.peak("jvm"),
        cpu_worker=max(workers, default=0),
        cpu_workers=workers,
        raster=sampler.peak("raster"),
        cli=cli,
    )


# ---- report -------------------------------------------------------------------------------


def _mb(n: int) -> str:
    return f"{n / MB:,.0f} MB"


def _cell(v: Any) -> str:
    text = json.dumps(v, default=str) if not isinstance(v, str) else v
    return text.replace("|", "\\|").replace("\n", " ")[:300]


def svg_stats(svg: Path) -> dict[str, int]:
    """Bytes and element count of a board SVG (the render limits, P1c B-C9)."""
    import xml.etree.ElementTree as ET

    try:
        size = svg.stat().st_size
        elements = sum(1 for _ in ET.parse(svg).iter())
    except (OSError, ET.ParseError):
        return {}
    return {"bytes": size, "elements": elements}


def build_report(
    result: Any,
    memory: Memory,
    *,
    project: str,
    error: str | None = None,
    seconds: float = 0.0,
    renders: Mapping[str, Mapping[str, int]] | None = None,
    after_failure: list[dict[str, Any]] | None = None,
) -> str:
    """`report.md`: steps, metrics, fill, open connections, fab files, renders, runtime,
    memory."""
    lines = [f"# Board flow dry run: {project}", ""]
    lines.append(f"Result: **{'FAILED' if error else 'completed'}**")
    if error:
        lines += ["", "```", error[:4000], "```"]
    lines += [
        "",
        "## Steps",
        "",
        "| Turn | Tool | Seconds | Cost | Result |",
        "|---|---|---|---|---|",
    ]
    for s in getattr(result, "steps", []) or []:
        cost = "" if s.cost is None else str(s.cost)
        lines.append(f"| {s.turn} | {s.tool} | {s.seconds:.1f} | {cost} | {_cell(s.summary)} |")
    metrics = getattr(result, "metrics", {}) or {}
    lines += ["", "## Metrics", ""]
    if metrics.get("metrics"):
        lines.append(
            f"Full check: hard failures {metrics.get('hard_failures')}, reviews "
            f"{metrics.get('reviews')}, verdict {metrics.get('verdict')}"
        )
        lines += ["", "| Metric | Value | Verdict | Offenders (first 3) |", "|---|---|---|---|"]
        for m in metrics["metrics"]:
            offs = "; ".join(str(o.get("what", "")) for o in (m.get("offenders") or [])[:3])
            lines.append(
                f"| {m['id']} | {_cell(m.get('value'))} | {m['verdict']} | {_cell(offs)} |"
            )
    else:
        lines.append("No full check reached.")
    if after_failure:
        lines += ["", "## After the failure (measurement only)", ""]
        for row in after_failure:
            state = "ok" if row["ok"] else f"error {_cell(row.get('error'))}"
            lines.append(f"- {row['tool']} {_cell(row['args'])}: {row['seconds']} s, {state}")
        full = next((r.get("metrics") for r in after_failure if r.get("metrics")), None)
        if full:
            lines += [
                "",
                f"Full check of the partial board: hard failures {full.get('hard_failures')}, "
                f"verdict {full.get('verdict')}",
                "",
                "| Metric | Value | Verdict | Offenders (first 3) |",
                "|---|---|---|---|",
            ]
            for m in full.get("metrics", []):
                offs = "; ".join(str(o.get("what", "")) for o in (m.get("offenders") or [])[:3])
                lines.append(
                    f"| {m['id']} | {_cell(m.get('value'))} | {m['verdict']} | {_cell(offs)} |"
                )
    fill = getattr(result, "fill", {}) or {}
    lines += ["", "## Fill", ""]
    if fill:
        lines.append(
            f"completion {fill.get('completion')}, connections {_cell(fill.get('connections'))}, "
            f"fill routed {fill.get('fill_routed')}, ripped {fill.get('ripped')}, rerouted "
            f"{fill.get('rerouted')}, seconds {fill.get('seconds')}"
        )
        opened = fill.get("open") or []
        lines += ["", f"Open connections after the fill: {len(opened)}"]
        lines += [f"- {o.get('connection')} at ({o.get('x')}, {o.get('y')})" for o in opened[:20]]
    else:
        lines.append("No fill reached.")
    lines += ["", "## Fab files", ""]
    files = getattr(result, "fab_files", []) or []
    lines += [f"- {f}" for f in files] or ["None (export not reached)."]
    if renders:
        lines += ["", "## Renders", "", "| PNG | SVG bytes | SVG elements |", "|---|---|---|"]
        for name, st in renders.items():
            lines.append(f"| {name} | {st.get('bytes', '')} | {st.get('elements', '')} |")
    lines += ["", "## Runtime", "", f"Total: {seconds:.0f} s"]
    by_tool: dict[str, float] = {}
    for s in getattr(result, "steps", []) or []:
        by_tool[s.tool] = by_tool.get(s.tool, 0.0) + s.seconds
    lines += [f"- {t}: {v:.1f} s" for t, v in sorted(by_tool.items(), key=lambda kv: -kv[1])]
    lines += ["", "## Memory", "", "Per active board project (D5 breakdown, five parts):", ""]
    lines.append(
        f"- api-server RSS (peak VmHWM): {_mb(memory.api_server)}"
        f" (before footprints {_mb(memory.api_before_fp)}, after {_mb(memory.api_after_fp)})"
    )
    lines.append(f"- JVM RSS (Freerouting, not heap): {_mb(memory.jvm)}")
    workers = ", ".join(_mb(w) for w in memory.cpu_workers) or "none"
    lines.append(f"- CPU worker VmHWM: {_mb(memory.cpu_worker)} (workers: {workers})")
    lines.append(f"- raster worker peak: {_mb(memory.raster)}")
    lines.append(f"- kicad-cli peak: {_mb(memory.cli_peak)} (largest kind; serialised)")
    for kind, v in sorted(memory.cli.items()):
        lines.append(f"  - {kind}: {_mb(v)}")
    total_gb = memory.total / (1024 * MB)
    lines += ["", f"**Sum per active board project: {_mb(memory.total)} ({total_gb:.2f} GB)**"]
    lo, hi = D5_GB
    within = "within" if total_gb <= hi else "ABOVE"
    lines.append(f"D5 figure: {lo}-{hi} GB per active board project → measured sum {within} it.")
    return "\n".join(lines) + "\n"


# ---- main ---------------------------------------------------------------------------------


AFTER_FAILURE = (  # read-only / approval-free calls: the kicad-cli kinds the flow missed
    ("pcb_check", {"level": "full"}),
    ("pcb_render", {"view": "top"}),
    ("pcb_render", {"view": "3d", "angle": "iso"}),
    ("pcb_export_3d", {"format": "step"}),
    ("pcb_export_3d", {"format": "glb"}),
)


async def after_failure(rig: Any, result: Any) -> list[dict[str, Any]]:
    """Best effort after a failed flow: the AFTER_FAILURE calls on the board as the flow
    left it - the full check (parity DRC) for the report, a top and a 3D render, the
    STEP / GLB export - so the memory report covers those kicad-cli kinds too. Returns
    one row per call; never raises."""
    from copperchat.projects import agent_dir
    from tests.kicad_e2e.flow import call

    rows: list[dict[str, Any]] = []
    for name, args in AFTER_FAILURE:
        t0 = time.monotonic()
        try:
            data, err = await call(rig, "run-dry-run-after-failure", name, **args)
        except Exception as exc:  # a measurement problem must not hide the flow's failure
            data, err = {"exception": repr(exc)}, True
        row: dict[str, Any] = {
            "tool": name,
            "args": args,
            "seconds": round(time.monotonic() - t0, 1),
            "ok": not err,
        }
        if err:
            row["error"] = data
        elif name == "pcb_check":
            row["metrics"] = data
        elif name == "pcb_render" and data.get("png_url") and result is not None:
            png = agent_dir(rig.project) / "renders" / str(data["png_url"]).rsplit("/", 1)[1]
            result.renders.append(png)
        rows.append(row)
    return rows


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--odh", required=True, type=Path, help="checkout (copied, never written)")
    p.add_argument("--project", required=True, help="relative path of the board's .kicad_pro")
    p.add_argument("--answers", required=True, type=Path, help="answers JSON (flow.Answers)")
    p.add_argument("--out", type=Path, default=None, help="output dir (outside the repo)")
    return p.parse_args(argv)


async def _run(args: argparse.Namespace, out: Path) -> int:
    from copperchat.config import Settings
    from copperchat.projects import discover_projects
    from tests.kicad_e2e.flow import FlowError, as_jsonable, open_rig, run_flow

    answers = load_answers(json.loads(args.answers.read_text()))
    copy = copy_checkout(args.odh)
    pro = (copy / args.project).resolve()
    if not pro.is_relative_to(copy.resolve()) or pro.suffix != ".kicad_pro" or not pro.is_file():
        raise UsageError(f"--project {args.project}: no .kicad_pro at that path in the checkout")
    root = copy.parent
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        projects_root=root,
        data_dir=root / ".data",
        admin_password="dry-run",
        claude_code_oauth_token="dry-run",
        cookie_secure=False,
        frontend_dist=root / ".no-frontend",
        kicad_cli=os.environ.get("KICAD_CLI") or "kicad-cli",
        kicad_api_timeout_s=30.0,
        kicad_start_timeout_s=120.0,
        board_tools=True,
    )
    project = next((p for p in discover_projects(root) if p.pro_file.resolve() == pro), None)
    if project is None:
        raise UsageError(f"--project {args.project}: not discovered as a KiCad project")
    me = os.getpid()
    sampler = PeakSampler(
        lambda: descendants(me), lambda pid: kind_of(proc_cmdline(pid), settings.kicad_cli)
    ).start()
    api: dict[str, int] = {}
    error: str | None = None
    extra: list[dict[str, Any]] = []
    start = time.monotonic()

    def step(s: Any) -> None:
        print(
            f"[{time.monotonic() - start:7.1f}s] turn {s.turn} {s.tool} {s.seconds:.1f}s "
            f"{json.dumps(s.summary, default=str)[:200]}",
            flush=True,
        )

    try:
        async with open_rig(settings, project, root / ".data" / "dry-run.db") as rig:
            try:
                result = await run_flow(rig, answers, on_step=step)
            except FlowError as exc:
                result, error = exc.result, str(exc)
                extra = await after_failure(rig, result)
            seconds = time.monotonic() - start
            proc = rig.session.process
            if proc is not None:
                api["before_fp"] = proc_status(proc.pid).get("VmHWM", 0)
                await rig.session.activate_footprints()
                proc = rig.session.process
                if proc is not None:
                    api["after_fp"] = proc_status(proc.pid).get("VmHWM", 0)
                    api["end"] = api["after_fp"]
            sampler.stop()
            memory = memory_from(sampler, api)
            out.mkdir(parents=True, exist_ok=True)
            renders: list[str] = []
            stats: dict[str, dict[str, int]] = {}
            for png in getattr(result, "renders", []) or []:
                if png.is_file():
                    dst = out / f"{len(renders) + 1:02d}-{png.name}"
                    shutil.copy2(png, dst)
                    renders.append(dst.name)
                    stats[dst.name] = svg_stats(png.with_suffix(".svg"))
            report = build_report(
                result,
                memory,
                project=args.project,
                error=error,
                seconds=seconds,
                renders=stats,
                after_failure=extra,
            )
            (out / "report.md").write_text(report)
            data = as_jsonable(result) if result is not None else {}
            data |= {"error": error, "memory": dataclasses.asdict(memory), "renders_out": renders}
            data["after_failure"] = extra
            (out / "metrics.json").write_text(json.dumps(data, indent=1, default=str))
    finally:
        sampler.stop()
    print(f"report: {out / 'report.md'} (copy of the checkout: {copy})")
    return 1 if error else 0


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        stamp = datetime.now(UTC).strftime("%Y-%m-%d")
        # no --out: a fresh private dir (a fixed /tmp name could be pre-created by others)
        out = refuse_out_inside_repo(
            args.out or Path(tempfile.mkdtemp(prefix=f"odh-dry-run-{stamp}-"))
        )
        return asyncio.run(_run(args, out))
    except UsageError as exc:
        print(f"odh-dry-run: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # the CPU worker pool spawns: never run the flow on import
    sys.exit(main())
