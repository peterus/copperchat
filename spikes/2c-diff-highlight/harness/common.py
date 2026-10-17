"""Shared helpers for spike 2C (runs inside the kicad-base image; spike-local)."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

FIXTURES = Path(os.environ.get("FIXTURES", "/w/backend/tests/fixtures/projects"))
OUT = Path(os.environ.get("OUT", "/w/spikes/2c-diff-highlight/results"))
KICAD_CLI = os.environ.get("KICAD_CLI", "kicad-cli")
ENV = dict(os.environ, LANG="C", LC_ALL="C")
ENV.setdefault("KICAD_CONFIG_HOME", tempfile.mkdtemp(prefix="c2-cfg-"))


def copy_fixture(name: str, dest: Path) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(FIXTURES / name, dest)
    return dest


class Server:
    def __init__(self, project_dir: Path):
        self.dir = project_dir
        self.sock = Path(tempfile.mkdtemp(prefix="c2-sock-")) / "api.sock"
        self.proc = None

    def __enter__(self):
        pro = next(self.dir.glob("*.kicad_pro"))
        self.log = open(self.sock.with_suffix(".log"), "wb")
        self.proc = subprocess.Popen(
            [KICAD_CLI, "api-server", "--socket", str(self.sock), str(pro)],
            env=ENV, cwd=self.dir, stdout=self.log, stderr=self.log, stdin=subprocess.DEVNULL)
        from kipy import KiCad
        from kipy.proto.common.types import DocumentType
        t0 = time.time()
        while True:
            try:
                self.kicad = KiCad(socket_path=f"ipc://{self.sock}", client_name="c2", timeout_ms=20000)
                self.kicad.ping()
                break
            except Exception:
                if time.time() - t0 > 60:
                    raise
                time.sleep(0.3)
        sch = next(p for p in self.dir.glob("*.kicad_sch") if p.stem == pro.stem)
        self.kicad.open_document(str(sch), DocumentType.DOCTYPE_SCHEMATIC)
        self.kicad.load_all_libraries()
        self.sch = self.kicad.get_schematic()
        return self

    def __exit__(self, *a):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except Exception:
                self.proc.kill()
