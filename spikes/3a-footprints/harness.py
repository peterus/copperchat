"""Shared spike helpers for 3A (runs inside the kicad-base image). Spike-local."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).parent
RESULTS = Path(os.environ.get("SPIKE_RESULTS", HERE / "results"))
KICAD_CLI = os.environ.get("KICAD_CLI", "/usr/bin/kicad-cli")
SETTINGS_VERSION = "10.99"
TEMPLATE = Path("/usr/share/kicad/template")


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


class Recorder:
    def __init__(self, question: str) -> None:
        self.question = question
        self.checks: list[dict] = []
        self.notes: dict[str, object] = {}

    def check(self, name: str, ok: bool, evidence: object) -> bool:
        print(f"{'PASS' if ok else 'FAIL'} {self.question}.{name}: {str(evidence)[:600]}", flush=True)
        self.checks.append({"name": name, "ok": bool(ok), "evidence": str(evidence)})
        return bool(ok)

    def note(self, name: str, value: object) -> None:
        print(f"NOTE {self.question}.{name}: {str(value)[:600]}", flush=True)
        self.notes[name] = value

    def finish(self) -> int:
        RESULTS.mkdir(parents=True, exist_ok=True)
        out = {"question": self.question, "checks": self.checks, "notes": self.notes}
        (RESULTS / f"{self.question}.json").write_text(json.dumps(out, indent=2, default=str))
        failed = [c["name"] for c in self.checks if not c["ok"]]
        print(f"DONE {self.question}: {len(self.checks) - len(failed)} pass, {len(failed)} fail")
        return 1 if failed else 0


class Server:
    """`kicad-cli api-server` (mirrors KicadCliLauncher) + kipy client.

    `project` may be None (server without a project, used to create documents)."""

    def __init__(self, project: Path | None, config_home: Path | None = None, wait_s: float = 60.0):
        self.project = project
        self.config_home = config_home or Path(tempfile.mkdtemp(prefix="c3a-cfg-"))
        self.wait_s = wait_s
        self.socket = Path(tempfile.mkdtemp(prefix="c3a-sock-")) / "api.sock"
        self.log_path = self.socket.parent / "server.log"
        self.proc: subprocess.Popen | None = None
        self.startup_s = -1.0

    def client(self, timeout_ms: int = 20000):
        from kipy import KiCad

        return KiCad(
            socket_path=f"ipc://{self.socket}",
            client_name=f"spike3a-{time.monotonic_ns()}",
            timeout_ms=timeout_ms,
        )

    def __enter__(self) -> "Server":
        env = env_for(self.config_home)
        log = self.log_path.open("w")
        args = [KICAD_CLI, "api-server", "--socket", str(self.socket)]
        if self.project is not None:
            args.append(str(self.project))
        t0 = time.monotonic()
        self.proc = subprocess.Popen(
            args, cwd=(self.project.parent if self.project else tempfile.gettempdir()),
            env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            while time.monotonic() - t0 < self.wait_s:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"api-server exited {self.proc.returncode}: {self.log()}")
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

    def log(self) -> str:
        return self.log_path.read_text(errors="replace")[-2000:] if self.log_path.exists() else ""

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def rss(self) -> float:
        return rss_mb(self.proc.pid) if self.alive() else -1.0

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


def cli(*args: str, config_home: Path | None = None, timeout: float = 120) -> subprocess.CompletedProcess:
    env = env_for(config_home or Path(tempfile.mkdtemp(prefix="c3a-cfg-")))
    return subprocess.run([KICAD_CLI, *args], env=env, capture_output=True, text=True, timeout=timeout)


def main_guard(fn) -> None:
    try:
        raise SystemExit(fn())
    except SystemExit:
        raise
    except Exception as e:  # a crash is a finding; print and fail
        import sys
        import traceback

        traceback.print_exc()
        print(f"FAIL crashed: {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1) from e
