"""Shared spike helpers for 2B (runs inside the kicad-base image). Spike-local."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).parent
RESULTS = Path(os.environ.get("SPIKE_RESULTS", HERE / "results"))
KICAD_CLI = os.environ.get("KICAD_CLI", "/usr/bin/kicad-cli")
SETTINGS_VERSION = "10.99"
TEMPLATE = Path("/usr/share/kicad/template")
DEMOS = Path("/usr/share/kicad/demos")


def env_for(config_home: Path) -> dict[str, str]:
    env = {**os.environ, "LANG": "C", "LC_ALL": "C", "KICAD_CONFIG_HOME": str(config_home)}
    target = config_home / SETTINGS_VERSION
    target.mkdir(parents=True, exist_ok=True)
    for name in ("sym-lib-table", "fp-lib-table"):  # same seeding as launcher.seed_library_tables
        if not (target / name).exists():
            shutil.copyfile(TEMPLATE / name, target / name)
    return env


def rss_mb(pid: int) -> float:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return -1.0


def fresh_project(demo: str = "ecc83") -> Path:
    """Copy a stock demo project to a tmp dir (bytes only; nothing is edited)."""
    dst = Path(tempfile.mkdtemp(prefix="c2b-")) / demo
    # ecc83 ships a second project (_v2) in the same dir; two .kicad_pro would be ambiguous
    shutil.copytree(DEMOS / demo, dst, ignore=shutil.ignore_patterns("*_v2.*"))
    return dst


class Recorder:
    def __init__(self, question: str) -> None:
        self.question = question
        self.checks: list[dict] = []
        self.notes: dict[str, object] = {}

    def check(self, name: str, ok: bool, evidence: object) -> bool:
        print(f"{'PASS' if ok else 'FAIL'} {self.question}.{name}: {str(evidence)[:400]}", flush=True)
        self.checks.append({"name": name, "ok": bool(ok), "evidence": str(evidence)})
        return bool(ok)

    def note(self, name: str, value: object) -> None:
        print(f"NOTE {self.question}.{name}: {str(value)[:400]}", flush=True)
        self.notes[name] = value

    def finish(self) -> int:
        RESULTS.mkdir(parents=True, exist_ok=True)
        out = {"question": self.question, "checks": self.checks, "notes": self.notes}
        (RESULTS / f"{self.question}.json").write_text(json.dumps(out, indent=2, default=str))
        failed = [c["name"] for c in self.checks if not c["ok"]]
        print(f"DONE {self.question}: {len(self.checks) - len(failed)} pass, {len(failed)} fail")
        return 1 if failed else 0


class Server:
    """`kicad-cli api-server` for one project (mirrors KicadCliLauncher) + kipy client."""

    def __init__(self, project_dir: Path, config_home: Path | None = None, wait_s: float = 60.0):
        self.project_dir = project_dir
        self.config_home = config_home or Path(tempfile.mkdtemp(prefix="c2b-cfg-"))
        self.wait_s = wait_s
        self.socket = Path(tempfile.mkdtemp(prefix="c2b-sock-")) / "api.sock"
        self.proc: subprocess.Popen | None = None
        self.startup_s = -1.0

    @property
    def pro(self) -> Path:
        return next(self.project_dir.glob("*.kicad_pro"))

    def client(self, timeout_ms: int = 5000):
        from kipy import KiCad

        return KiCad(
            socket_path=f"ipc://{self.socket}",
            client_name=f"spike2b-{time.monotonic_ns()}",
            timeout_ms=timeout_ms,
        )

    def __enter__(self) -> "Server":
        env = env_for(self.config_home)
        log = (self.socket.parent / "server.log").open("w")
        t0 = time.monotonic()
        self.proc = subprocess.Popen(
            [KICAD_CLI, "api-server", "--socket", str(self.socket), str(self.pro)],
            cwd=self.project_dir, env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            while time.monotonic() - t0 < self.wait_s:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"api-server exited {self.proc.returncode}")
                if self.socket.exists():
                    try:
                        self.client(1000).ping()
                        self.startup_s = time.monotonic() - t0
                        return self
                    except Exception:  # noqa: BLE001, S110 - polling
                        pass
                time.sleep(0.1)
            raise TimeoutError("api-server not ready")
        except BaseException:
            self.stop()
            raise

    def open_schematic(self) -> None:
        from kipy.proto.common.types import DocumentType

        sch = self.pro.with_suffix(".kicad_sch")
        self.client(60000).open_document(str(sch), DocumentType.DOCTYPE_SCHEMATIC)

    def open_board(self) -> None:
        from kipy.proto.common.types import DocumentType

        pcb = self.pro.with_suffix(".kicad_pcb")
        self.client(60000).open_document(str(pcb), DocumentType.DOCTYPE_PCB)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)

    def __exit__(self, *exc: object) -> None:
        self.stop()


def main_guard(fn) -> None:
    try:
        raise SystemExit(fn())
    except SystemExit:
        raise
    except Exception as e:  # a crash is a finding; print and fail
        import sys

        print(f"FAIL crashed: {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1) from e
