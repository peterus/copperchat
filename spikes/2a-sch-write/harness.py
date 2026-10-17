"""Shared spike helpers (spike 2A). Spike-local; not imported by the backend.

Style follows spikes/1c-api-server/harness.py. Runs inside the pinned kicad-base image
(`./run.sh python qNN_*.py`): kicad-cli on PATH, kipy in /opt/venv.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Self

HERE = Path(__file__).parent
RESULTS = HERE / "results"
FILES = RESULTS / "files"  # artifacts (saved schematics) for the KiCanvas check; gitignored
FIXTURES = HERE / "fixtures"
CONFIG_HOME = HERE / ".kicad/config"
KICAD_SETTINGS_VERSION = "10.99"
MM = 1_000_000  # kipy coordinates are nm


def kicad_cli() -> str:
    return os.environ.get("KICAD_CLI") or shutil.which("kicad-cli") or "/nonexistent"


def require_kicad() -> None:
    if not Path(kicad_cli()).exists():
        print("SKIP: kicad-cli with api-server not found")
        raise SystemExit(0)


def seed_library_tables() -> None:
    """Byte-copy KiCad's stock global lib tables into the isolated config (as backend
    launcher.seed_library_tables does)."""
    exe = shutil.which(kicad_cli())
    if exe is None:
        return
    template = Path(exe).resolve().parent.parent / "share/kicad/template"
    target = CONFIG_HOME / KICAD_SETTINGS_VERSION
    for name in ("sym-lib-table", "fp-lib-table"):
        if (template / name).is_file() and not (target / name).exists():
            target.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(template / name, target / name)


def cli_env() -> dict[str, str]:
    env = dict(os.environ)
    env["LANG"] = "C"
    env["LC_ALL"] = "C"
    env["KICAD_CONFIG_HOME"] = str(CONFIG_HOME)
    seed_library_tables()
    return env


def fresh_fixture(name: str) -> Path:
    """Copy fixtures/<name> to a fresh tmp dir; returns the project dir."""
    src = FIXTURES / name
    if not src.exists():
        print(f"FAIL: fixture {name} missing - run ./run.sh ./make_fixtures.sh")
        raise SystemExit(1)
    dst = Path(tempfile.mkdtemp(prefix=f"s2a-{name}-")) / name
    shutil.copytree(src, dst)
    return dst


def project_file(project_dir: Path) -> Path:
    return next(project_dir.glob("*.kicad_pro"))


def root_sch(project_dir: Path) -> Path:
    return project_dir / f"{project_file(project_dir).stem}.kicad_sch"


def keep_artifact(project_dir: Path, name: str) -> Path:
    """Copy a saved project into results/files/<name> (input of the KiCanvas check)."""
    dst = FILES / name
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(
        project_dir, dst, ignore=shutil.ignore_patterns("*.lck", "*.kicad_pcb", "*-backups")
    )
    return dst


class Recorder:
    def __init__(self, question: str) -> None:
        self.question = question
        self.checks: list[dict] = []
        self.notes: dict[str, object] = {}

    def check(self, name: str, ok: bool, evidence: object) -> bool:
        print(f"{'PASS' if ok else 'FAIL'} {self.question}.{name}: {str(evidence)[:400]}")
        self.checks.append({"name": name, "ok": bool(ok), "evidence": str(evidence)})
        return bool(ok)

    def note(self, name: str, evidence: object) -> None:
        print(f"NOTE {self.question}.{name}: {str(evidence)[:400]}")
        self.notes[name] = evidence

    def attempt(self, name: str, fn, expect_ok: bool = True):
        """Run fn(); a raised exception is evidence. Returns (ok, value_or_exc)."""
        try:
            v = fn()
            self.check(name, expect_ok, f"ok: {str(v)[:300]}")
            return True, v
        except Exception as e:  # noqa: BLE001 - every API error is a finding
            self.check(name, not expect_ok, f"{type(e).__name__}: {e}")
            return False, e

    def finish(self) -> int:
        RESULTS.mkdir(exist_ok=True)
        out = {"question": self.question, "checks": self.checks, "notes": self.notes}
        (RESULTS / f"{self.question}.json").write_text(json.dumps(out, indent=2, default=str))
        failed = [c["name"] for c in self.checks if not c["ok"]]
        print(f"DONE {self.question}: {len(self.checks) - len(failed)} pass, {len(failed)} fail")
        return 1 if failed else 0


class Server:
    """Starts `kicad-cli api-server --socket S [PROJECT]`; waits until kipy can ping it.

    `project_dir=None` starts the server without a project (for CreateDocument).
    """

    def __init__(self, project_dir: Path | None, wait_s: float = 60.0, open_docs: bool = True):
        self.project_dir = project_dir
        self.open_docs = open_docs
        self.open_s: dict[str, float] = {}
        self.socket = Path(tempfile.mkdtemp(prefix="s2a-sock-")) / "api.sock"
        self.url = f"ipc://{self.socket}"
        self.wait_s = wait_s
        self.proc: subprocess.Popen | None = None
        self.startup_s = -1.0
        self.log = self.socket.with_suffix(".log")
        self._log_fh = None

    def __enter__(self) -> Self:
        try:
            return self._start()
        except BaseException:
            self.stop()
            raise

    def _start(self) -> Self:
        cmd = [kicad_cli(), "api-server", "--socket", str(self.socket)]
        if self.project_dir is not None:
            cmd.append(str(project_file(self.project_dir)))
        t0 = time.monotonic()
        self._log_fh = self.log.open("w")
        self.proc = subprocess.Popen(
            cmd,
            cwd=self.project_dir or tempfile.gettempdir(),
            env=cli_env(),
            stdout=self._log_fh,
            stderr=subprocess.STDOUT,
        )
        while time.monotonic() - t0 < self.wait_s:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"api-server exited {self.proc.returncode}: {self.log.read_text()}"
                )
            if self.socket.exists():
                try:
                    self.client(timeout_ms=1000).ping()
                    self.startup_s = time.monotonic() - t0
                    break
                except Exception:  # noqa: BLE001, S110 - polling until ready
                    pass
            time.sleep(0.1)
        else:
            raise TimeoutError(f"api-server not ready after {self.wait_s}s: {self.log.read_text()}")
        if self.open_docs and self.project_dir is not None:
            self.open_schematic()
        return self

    def open_schematic(self) -> None:
        from kipy.proto.common.types import DocumentType

        assert self.project_dir is not None
        t0 = time.monotonic()
        self.client(timeout_ms=60000).open_document(
            str(root_sch(self.project_dir)), DocumentType.DOCTYPE_SCHEMATIC
        )
        self.open_s["kicad_sch"] = time.monotonic() - t0

    def client(self, timeout_ms: int = 20000, name: str | None = None):
        from kipy import KiCad

        return KiCad(
            socket_path=self.url,
            client_name=name or f"spike2a-{time.monotonic_ns()}",
            timeout_ms=timeout_ms,
        )

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            os.kill(self.proc.pid, signal.SIGCONT)
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)
        if self._log_fh is not None:
            self._log_fh.close()
            self._log_fh = None

    def __exit__(self, *exc: object) -> None:
        self.stop()


def load_libraries(k, timeout_s: float = 300.0) -> float:
    """LoadAllLibraries + wait; returns seconds. Required before PlaceSymbolFromLibrary."""
    t0 = time.monotonic()
    k.load_all_libraries()
    k.wait_for_libraries(timeout=timeout_s) if _accepts_timeout(k) else k.wait_for_libraries()
    return time.monotonic() - t0


def _accepts_timeout(k) -> bool:
    import inspect

    return "timeout" in inspect.signature(k.wait_for_libraries).parameters


def run_cli(*args: str, cwd: Path | None = None, timeout: float = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        [kicad_cli(), *args],
        check=False,
        cwd=cwd,
        env=cli_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------- kipy helpers


def v2(x_mm: float, y_mm: float):
    from kipy.geometry import Vector2

    return Vector2.from_xy(round(x_mm * MM), round(y_mm * MM))


def mm(v) -> tuple[float, float]:
    return (round(v.x / MM, 4), round(v.y / MM, 4))


def new_kiid():
    from kipy.proto.common.types import KIID

    k = KIID()
    k.value = str(uuid.uuid4())
    return k


def sid(item) -> str:
    return str(item.id.value)


def by_id(sch, ids):
    """GetItemsById (whole hierarchy); returns {uuid: item}."""
    from kipy.proto.common.types import KIID

    kiids = []
    for i in ids:
        k = KIID()
        k.value = i if isinstance(i, str) else i.value
        kiids.append(k)
    try:
        return {sid(it): it for it in sch.get_items_by_id(kiids)}
    except Exception:  # noqa: BLE001 - one missing id fails the whole call; retry singly
        out = {}
        for k in kiids:
            try:
                for it in sch.get_items_by_id([k]):
                    out[sid(it)] = it
            except Exception:  # noqa: BLE001, S112
                continue
        return out


def update_symbols(sch, syms):
    """update_items for placed symbols WITHOUT their definition (Q2 finding: a full-definition
    update of a second instance of a multi-unit symbol hangs the headless server; the
    definition-less update keeps the existing library link and pins)."""
    for s in syms:
        s.proto.ClearField("definition")
    return sch.update_items(list(syms))


def wire(start, end):
    from kipy.proto.schematic.schematic_types_pb2 import SchematicLineType
    from kipy.schematic_types import SchematicLine

    w = SchematicLine()
    w.start = v2(*start)
    w.end = v2(*end)
    w.type = SchematicLineType.SLT_WIRE
    return w


def local_label(text, at, angle=0.0):
    from kipy.schematic_types import LocalLabel

    lb = LocalLabel()
    lb.text.value = text
    lb.position = v2(*at)
    lb.text.position = v2(*at)
    return lb


def junction(at):
    from kipy.schematic_types import Junction

    j = Junction()
    j.position = v2(*at)
    return j


def no_connect(at):
    from kipy.schematic_types import NoConnectMarker

    n = NoConnectMarker()
    n.position = v2(*at)
    return n


def pin_ends(sym) -> dict[str, tuple[float, float]]:
    """Absolute connection points of a placed symbol's pins, as the API reports them."""
    return {p.number: mm(p.position) for p in sym.definition.pins}


def nets_by_pin(sch) -> dict[tuple[str, str], str]:
    """(ref, pin) -> net name from the GetSchematicNetlist + GetItems of symbols.

    The netlist lists item KIIDs per sheet; pins are mapped back through the symbols'
    definition pins (pin KIIDs).
    """
    pin_owner: dict[str, tuple[str, str]] = {}
    for s in sch.get_symbols():
        for p in s.definition.pins:
            pin_owner[sid(p)] = (s.reference, p.number)
    out = {}
    for net in sch.get_netlist():
        for sheet in net.sheets:
            for item in sheet.items:
                key = pin_owner.get(str(item.value))
                if key:
                    out[key] = net.name
    return out


def same_net(net: str | None, label: str) -> bool:
    """Local-label nets are named '/<label>' (sheet path prefix)."""
    return net is not None and (net == label or net.rsplit("/", 1)[-1] == label)


def cli_netlist(sch_file: Path, out_dir: Path) -> dict[tuple[str, str], str]:
    """kicad-cli sch export netlist (kicadsexpr) parsed for (ref, pin) -> net. Reads
    KiCad's own output only (no KiCad file is written)."""
    out = out_dir / "cli.net"
    r = run_cli("sch", "export", "netlist", "-o", str(out), str(sch_file))
    if r.returncode != 0:
        raise RuntimeError(r.stdout + r.stderr)
    return parse_sexpr_netlist(out.read_text())


def sexpr(text: str):
    """Minimal S-expression reader for KiCad's *output* (netlists); never used to write."""
    import re

    tokens = re.findall(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+', text)
    stack: list[list] = [[]]
    for t in tokens:
        if t == "(":
            stack.append([])
        elif t == ")":
            done = stack.pop()
            stack[-1].append(done)
        else:
            stack[-1].append(t[1:-1].replace('\\"', '"') if t.startswith('"') else t)
    return stack[0][0]


def _child(node, key):
    return next((c for c in node if isinstance(c, list) and c and c[0] == key), None)


def parse_sexpr_netlist(text: str) -> dict[tuple[str, str], str]:
    root = sexpr(text)
    res = {}
    for net in _child(root, "nets")[1:]:
        name = _child(net, "name")[1]
        for node in net:
            if isinstance(node, list) and node and node[0] == "node":
                res[(_child(node, "ref")[1], _child(node, "pin")[1])] = name
    return res


def netlist_components(text: str) -> dict[str, dict]:
    """ref -> {value, footprint, fields{}, libsource} from a kicadsexpr netlist."""
    root = sexpr(text)
    out = {}
    for comp in _child(root, "components")[1:]:
        ref = _child(comp, "ref")[1]
        d = {k: (_child(comp, k) or [None, None])[1] for k in ("value", "footprint")}
        flds = _child(comp, "fields")
        d["fields"] = {f[1][1]: (f[2] if len(f) > 2 else "") for f in (flds or [])[1:] if isinstance(f, list)}
        lib = _child(comp, "libsource")
        d["lib"] = f"{_child(lib, 'lib')[1]}:{_child(lib, 'part')[1]}" if lib else None
        d["props"] = {p[1][1]: (p[2][1] if len(p) > 2 else "") for p in comp if isinstance(p, list) and p and p[0] == "property"}
        out[ref] = d
    return out


def job_netlist(sch, out_dir: Path) -> dict[tuple[str, str], str]:
    """Netlist export job through the API (what backend sch_get_net does)."""
    out = out_dir / f"job-{time.monotonic_ns()}.net"
    res = sch.export_netlist(str(out))
    if not out.exists():
        raise RuntimeError(f"export_netlist wrote nothing: {res}")
    return parse_sexpr_netlist(out.read_text())


@contextmanager
def commit(board_or_sch, message: str = "spike 2A"):
    c = board_or_sch.begin_commit()
    try:
        yield c
    except BaseException:
        board_or_sch.drop_commit(c)
        raise
    else:
        board_or_sch.push_commit(c, message)


def main_guard(fn) -> None:
    try:
        raise SystemExit(fn())
    except SystemExit:
        raise
    except Exception as e:  # a crash is a finding; print and fail
        import traceback

        traceback.print_exc()
        print(f"FAIL crashed: {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1) from e
