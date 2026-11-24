"""Q7: showing the board — per-layer + composite images (kicad-cli svg, rasterised on the host),
3D render (kicad-cli pcb render, headless), STEP/GLB export, in-memory board string via the API
(SaveDocumentToString) as a source for a live KiCanvas view."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from kipy.proto.common.commands import editor_commands_pb2

import boards
import flow
import freerouting
from harness import RESULTS, Recorder, Server, cli, main_guard


def timed_cli(*args):
    t0 = time.monotonic()
    r = cli(*args, timeout=900)
    return r, round(time.monotonic() - t0, 2)


def run() -> int:
    r = Recorder("q07_show")
    base = Path(tempfile.mkdtemp(prefix="q07-"))
    p, net = flow.prepare(base, "ecc83", None)
    with Server(p.pro) as srv:
        k = srv.client(300000)
        b, parts, info = flow.build(k, p, net, "ecc83")
        freerouting.export_dsn_cli(p.pcb, p.dir / "b.dsn")
        freerouting.route(p.dir / "b.dsn", p.dir / "b.ses")
        freerouting.import_ses(b, p.dir / "b.ses")
        b.save()
        # in-memory string of the live board (what a mid-turn KiCanvas refresh could load)
        t0 = time.monotonic()
        try:
            s = b.client.send(editor_commands_pb2.SaveDocumentToString(document=b.document),
                              editor_commands_pb2.SavedDocumentResponse)
            r.check("save_document_to_string", s.contents.startswith("(kicad_pcb"),
                    f"{len(s.contents)} chars in {time.monotonic() - t0:.3f}s")
        except Exception as e:  # noqa: BLE001
            r.check("save_document_to_string", False, repr(e))

    # per-layer and composite images
    secs = []
    for layers, tag in (("F.Cu,Edge.Cuts", "top_cu"), ("B.Cu,Edge.Cuts", "bottom_cu"),
                        ("B.Cu,F.Cu,F.SilkS,Edge.Cuts", "composite"), ("F.Fab,F.CrtYd,Edge.Cuts", "assembly")):
        secs.append(boards.render(p.pcb, RESULTS / f"q07_ecc83_{tag}.svg", layers=layers))
    r.check("svg_layers_and_composite", True, f"4 svgs, {[round(x, 2) for x in secs]} s each")
    res, t = timed_cli("pcb", "render", "-o", str(RESULTS / "q07_ecc83_3d_top.png"), "--width", "1200", "--height", "900",
                       "--side", "top", "--quality", "basic", str(p.pcb))
    r.check("render_3d_top", (RESULTS / "q07_ecc83_3d_top.png").exists(), f"{t}s rc={res.returncode}")
    res, t = timed_cli("pcb", "render", "-o", str(RESULTS / "q07_ecc83_3d_iso.png"), "--width", "1200", "--height", "900",
                       "--quality", "high", "--rotate", "-45,0,45", "--perspective", "--floor", str(p.pcb))
    r.check("render_3d_iso_high", (RESULTS / "q07_ecc83_3d_iso.png").exists(), f"{t}s rc={res.returncode}")

    step = p.dir / "board.step"
    res, t = timed_cli("pcb", "export", "step", "-f", "-o", str(step), str(p.pcb))
    warn = [ln for ln in (res.stdout + res.stderr).splitlines() if "not found" in ln.lower() or "could not" in ln.lower()]
    r.check("step_export", step.exists() and step.stat().st_size > 10000,
            f"{t}s {step.stat().st_size if step.exists() else 0} bytes; missing-model lines={len(warn)} e.g. {warn[:2]}")
    r.note("models_in_image", "no /usr/share/kicad/3dmodels: component bodies missing in STEP and 3D render")
    glb = p.dir / "board.glb"
    res, t = timed_cli("pcb", "export", "glb", "-f", "-o", str(glb), str(p.pcb))
    r.check("glb_export", glb.exists(), f"{t}s {glb.stat().st_size if glb.exists() else 0} bytes")

    # OpenDriveHub placed (4 layers): composite of all copper + 3D
    o, net = flow.prepare(base, "odh", None)
    with Server(o.pro) as srv:
        k = srv.client(600000)
        flow.build(k, o, net, "odh")
    t = boards.render(o.pcb, RESULTS / "q07_odh_assembly.svg", layers="F.Fab,F.CrtYd,F.SilkS,Edge.Cuts")
    res, t3 = timed_cli("pcb", "render", "-o", str(RESULTS / "q07_odh_3d_iso.png"), "--width", "1200", "--height", "900",
                        "--quality", "basic", "--rotate", "-45,0,45", "--perspective", str(o.pcb))
    r.check("odh_3d", (RESULTS / "q07_odh_3d_iso.png").exists(), f"svg {t:.2f}s, 3d {t3}s")
    step = o.dir / "board.step"
    res, t = timed_cli("pcb", "export", "step", "-f", "-o", str(step), str(o.pcb))
    r.check("odh_step", step.exists(), f"{t}s {step.stat().st_size if step.exists() else 0} bytes")
    return r.finish()


if __name__ == "__main__":
    main_guard(run)
