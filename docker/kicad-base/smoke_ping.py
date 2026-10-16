"""Start kicad-cli api-server on a copy of the ecc83 demo and ping it via kipy.

Installed in the base image as /opt/copperchat/smoke_ping.py and run by
scripts/kicad-base-smoke.sh. The installed demo is copied to a temp dir and
never edited. Calls follow docs/spikes/1c-api-server.md (api-server syntax,
socket URL, open_document after connect, schematic/board accessors).
"""

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEMO = Path("/usr/share/kicad/demos/ecc83")
STEM = "ecc83-pp"
READY_TIMEOUT_S = 90


def main() -> int:
    from kipy import KiCad
    from kipy.proto.common.types import DocumentType

    tmp = Path(tempfile.mkdtemp(prefix="smoke-"))
    work = tmp / "ecc83"
    shutil.copytree(DEMO, work)
    pro = work / f"{STEM}.kicad_pro"
    sock = tmp / "api.sock"
    url = f"ipc://{sock}"
    proc = subprocess.Popen(
        ["kicad-cli", "api-server", "--socket", str(sock), str(pro)],
        cwd=work,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < READY_TIMEOUT_S:
            if proc.poll() is not None:
                print(f"api-server exited {proc.returncode}: {proc.stdout.read()}")
                return 1
            if sock.exists():
                try:
                    KiCad(socket_path=url, client_name="smoke-poll", timeout_ms=1000).ping()
                    break
                except Exception:  # noqa: BLE001 - polling until ready
                    pass
            time.sleep(0.2)
        else:
            print(f"api-server not ready after {READY_TIMEOUT_S}s")
            return 1
        print(f"api-server ready after {time.monotonic() - t0:.1f}s")

        k = KiCad(socket_path=url, client_name="smoke", timeout_ms=60000)
        print(f"kicad version: {k.get_version()}")
        # The headless server only preloads the project; documents must be opened.
        k.open_document(str(work / f"{STEM}.kicad_sch"), DocumentType.DOCTYPE_SCHEMATIC)
        k.open_document(str(work / f"{STEM}.kicad_pcb"), DocumentType.DOCTYPE_PCB)
        n_sym = len(k.get_schematic().get_symbols())
        n_fp = len(k.get_board().get_footprints())
        print(f"symbols={n_sym} footprints={n_fp}")
        return 0 if n_sym > 0 and n_fp > 0 else 1
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
