"""Specctra round trip: DSN export (kicad-cli or API job) -> Freerouting 2.5.0 (JAR on Java 25,
headless) -> ImportSpecctraSession via the API."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from kipy.proto.board import board_commands_pb2, board_jobs_pb2
from kipy.proto.common.types import jobs_pb2

from harness import HERE, cli

JAVA = HERE / ".deps" / "jre" / "bin" / "java"
JAR = HERE / ".deps" / "freerouting" / "freerouting-2.5.0.jar"


def export_dsn_cli(pcb: Path, dsn: Path) -> float:
    t0 = time.monotonic()
    r = cli("pcb", "export", "specctra", "-o", str(dsn), str(pcb))
    if not dsn.exists():
        raise RuntimeError(r.stdout + r.stderr)
    return time.monotonic() - t0


def export_dsn_api(board, dsn: Path):
    cmd = board_jobs_pb2.RunBoardJobExportSpecctraDsn()
    cmd.job_settings.document.CopyFrom(board.document)
    cmd.job_settings.output_path = str(dsn)
    return board.client.send(cmd, jobs_pb2.RunJobResponse)


def route(dsn: Path, ses: Path, *, passes: int = 30, extra: tuple[str, ...] = (), timeout: float = 1800) -> dict:
    t0 = time.monotonic()
    log = ses.with_suffix(".log")
    args = [str(JAVA), "-jar", str(JAR), "-de", str(dsn), "-do", str(ses),
            f"--router.autorouter.max_passes={passes}", "--gui.enabled=false", *extra]
    with log.open("w") as fh:
        p = subprocess.run(args, stdout=fh, stderr=subprocess.STDOUT, timeout=timeout, cwd=dsn.parent,
                           env={"HOME": "/tmp", "PATH": "/usr/bin:/bin"})
    txt = log.read_text(errors="replace")
    summary = [ln.split("] ", 1)[-1] for ln in txt.splitlines()
               if "stage completed" in ln or "Skipping optimization" in ln or "Optimization stage" in ln
               or "finished with state" in ln or "unrouted" in ln and "pass #" in ln][-6:]
    return {"rc": p.returncode, "seconds": round(time.monotonic() - t0, 1), "ses": ses.exists(), "log": summary}


def import_ses(board, ses: Path):
    cmd = board_commands_pb2.ImportSpecctraSession(board=board.document, session_path=str(ses))
    return board.client.send(cmd, board_commands_pb2.ImportSpecctraSessionResponse)
