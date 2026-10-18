#!/usr/bin/env python3
"""Creates the `blank` test fixture with KiCad itself (never by hand).

Runs inside the KiCad base image (kicad-cli with api-server, kipy in /opt/venv):
    /opt/venv/bin/python scripts/make-blank-fixture.py [FIXTURES_DIR]

- `blank.kicad_pro`: KiCad's project template, copied verbatim.
- `blank.kicad_sch`, `blank.kicad_pcb`: created and saved by a headless api-server
  (CreateDocument + SaveDocument; as spikes/2a-sch-write/make_blank.py).
- `ecc83-pp.kicad_sym`, `sym-lib-table`: the ecc83 fixture's project library and its
  table (`${KIPRJMOD}`), copied verbatim, so the fixture has a project library.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from kipy import KiCad
from kipy.proto.common.types import DocumentType

TEMPLATE = Path("/usr/share/kicad/template/kicad.kicad_pro")
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEST = ROOT / "backend/tests/fixtures/projects"


def _wait_for_server(proc: subprocess.Popen[bytes], url: str, timeout_s: float) -> KiCad:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"api-server exited with code {proc.returncode}")
        try:
            client = KiCad(socket_path=url, client_name="make-blank", timeout_ms=60_000)
            client.ping()
            return client
        except Exception:  # not ready yet
            time.sleep(0.2)
    raise RuntimeError(f"api-server not ready after {timeout_s:.0f}s")


def make_blank(dest_root: Path) -> Path:
    ecc83 = dest_root / "ecc83"
    if not (ecc83 / "ecc83-pp.kicad_sym").is_file():
        raise SystemExit(f"{ecc83} has no ecc83-pp.kicad_sym; run refresh-fixtures.sh first")
    with tempfile.TemporaryDirectory(prefix="cc-blank-") as tmp:
        work = Path(tmp) / "blank"
        work.mkdir()
        socket = Path(tmp) / "api.sock"
        url = f"ipc://{socket}"
        env = dict(os.environ, LANG="C", LC_ALL="C", KICAD_CONFIG_HOME=str(Path(tmp) / "config"))
        proc = subprocess.Popen(
            ["kicad-cli", "api-server", "--socket", str(socket)],
            cwd=tmp,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            client = _wait_for_server(proc, url, 60)
            client.create_document(str(work / "blank.kicad_sch"), DocumentType.DOCTYPE_SCHEMATIC)
            client.get_schematic().save()
            client.create_document(str(work / "blank.kicad_pcb"), DocumentType.DOCTYPE_PCB)
            client.get_board().save()
        finally:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        shutil.copyfile(TEMPLATE, work / "blank.kicad_pro")
        for name in ("ecc83-pp.kicad_sym", "sym-lib-table"):
            shutil.copyfile(ecc83 / name, work / name)
        dest = dest_root / "blank"
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(
            work, dest, ignore=shutil.ignore_patterns("*.lck", "*.kicad_prl", "*-backups")
        )
    return dest


if __name__ == "__main__":
    out = make_blank(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DEST)
    print("blank:", sorted(p.name for p in out.iterdir()))
