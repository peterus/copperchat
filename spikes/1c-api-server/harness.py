"""Shared spike helpers. Spike-local; not imported by the backend."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).parent
RESULTS = HERE / "results"
FIXTURES = HERE / "fixtures"
API_SERVER_ARGS = os.environ.get("KICAD_API_SERVER_ARGS", "api-server {project} --socket {socket}")


def kicad_cli() -> str:
    env = os.environ.get("KICAD_CLI")
    if env:
        return env
    direct = HERE / ".kicad/squashfs-root/usr/bin/kicad-cli"
    return str(direct)


def require_kicad() -> None:
    if not Path(kicad_cli()).exists():
        print("SKIP: kicad-cli with api-server not found")
        raise SystemExit(0)


def cli_env() -> dict[str, str]:
    env = dict(os.environ)
    env["LANG"] = "C"
    env["LC_ALL"] = "C"
    # Keep KiCad's settings out of the user's ~/.config/kicad (spike-local, gitignored).
    env.setdefault("KICAD_CONFIG_HOME", str(HERE / ".kicad/config"))
    appdir = HERE / ".kicad/squashfs-root"
    if appdir.exists():
        libdirs = [str(appdir / "usr/lib"), str(appdir / "usr/lib/x86_64-linux-gnu")]
        env["LD_LIBRARY_PATH"] = ":".join(libdirs + [env.get("LD_LIBRARY_PATH", "")])
    return env


def fresh_fixture(name: str) -> Path:
    """Copy fixtures/<name> to a fresh tmp dir; returns the project dir."""
    src = FIXTURES / name
    if not src.exists():
        print(f"FAIL: fixture {name} missing - run ./make_fixtures.sh")
        raise SystemExit(1)
    dst = Path(tempfile.mkdtemp(prefix=f"c1-{name}-")) / name
    shutil.copytree(src, dst)
    return dst


def project_file(project_dir: Path) -> Path:
    return next(project_dir.glob("*.kicad_pro"))


class Recorder:
    def __init__(self, question: str) -> None:
        self.question = question
        self.checks: list[dict] = []
        self.notes: dict[str, object] = {}

    def check(self, name: str, ok: bool, evidence: object) -> bool:
        print(f"{'PASS' if ok else 'FAIL'} {self.question}.{name}: {str(evidence)[:300]}")
        self.checks.append({"name": name, "ok": bool(ok), "evidence": str(evidence)})
        return bool(ok)

    def note(self, name: str, evidence: object) -> None:
        print(f"NOTE {self.question}.{name}: {str(evidence)[:300]}")
        self.notes[name] = evidence

    def finish(self) -> int:
        RESULTS.mkdir(exist_ok=True)
        out = {"question": self.question, "checks": self.checks, "notes": self.notes}
        (RESULTS / f"{self.question}.json").write_text(json.dumps(out, indent=2, default=str))
        failed = [c["name"] for c in self.checks if not c["ok"]]
        print(f"DONE {self.question}: {len(self.checks) - len(failed)} pass, {len(failed)} fail")
        return 1 if failed else 0


class Server:
    """Starts `kicad-cli api-server` for one project; waits until kipy can ping it."""

    def __init__(
        self, project_dir: Path, socket: Path | None = None, wait_s: float = 60.0,
        open_docs: bool = True,
    ):
        self.project_dir = project_dir
        self.open_docs = open_docs
        self.open_s: dict[str, float] = {}
        self.socket = socket or Path(tempfile.mkdtemp(prefix="c1-sock-")) / "api.sock"
        self.url = f"ipc://{self.socket}"
        self.wait_s = wait_s
        self.proc: subprocess.Popen | None = None
        self.startup_s = -1.0
        self.log = self.socket.with_suffix(".log")

    def __enter__(self) -> Server:
        args = API_SERVER_ARGS.format(project=project_file(self.project_dir), socket=self.socket)
        cmd = [kicad_cli(), *shlex.split(args)]
        t0 = time.monotonic()
        self.proc = subprocess.Popen(
            cmd, cwd=self.project_dir, env=cli_env(), stdout=self.log.open("w"),
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
                except Exception:  # noqa: BLE001 - polling until ready
                    pass
            time.sleep(0.1)
        else:
            self.stop()
            raise TimeoutError(f"api-server not ready after {self.wait_s}s: {self.log.read_text()}")
        if self.open_docs:
            self.open_documents()
        return self

    def open_documents(self) -> None:
        """Headless api-server preloads only the project; sch/pcb need OpenDocument."""
        from kipy.proto.common.types import DocumentType

        k = self.client(timeout_ms=60000)
        stem = project_file(self.project_dir).stem
        for ext, doctype in (("kicad_sch", DocumentType.DOCTYPE_SCHEMATIC),
                             ("kicad_pcb", DocumentType.DOCTYPE_PCB)):
            f = self.project_dir / f"{stem}.{ext}"
            if f.exists():
                t0 = time.monotonic()
                k.open_document(str(f), doctype)
                self.open_s[ext] = time.monotonic() - t0

    def client(self, timeout_ms: int = 5000):
        from kipy import KiCad

        return KiCad(
            socket_path=self.url, client_name=f"spike1c-{time.monotonic_ns()}",
            timeout_ms=timeout_ms,
        )

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


def run_cli(*args: str, cwd: Path | None = None, timeout: float = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        [kicad_cli(), *args], cwd=cwd, env=cli_env(), capture_output=True, text=True,
        timeout=timeout,
    )


def main_guard(fn) -> None:  # used by every q*.py
    try:
        raise SystemExit(fn())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - a crash is a finding; print and fail
        print(f"FAIL crashed: {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1) from e
