"""Q8: JLCPCB fab outputs via kicad-cli — Gerbers (incl. paste for the stencil, edge cuts), Excellon drill
(PTH/NPTH), position file -> JLC CPL, BOM -> JLC BOM, zip. Runs on ecc83 (routed) and OpenDriveHub (placed)."""

from __future__ import annotations

import csv
import io
import tempfile
import time
import zipfile
from pathlib import Path

import flow
import freerouting
from harness import RESULTS, Recorder, Server, cli, main_guard

GERBER_LAYERS = "F.Cu,B.Cu,In1.Cu,In2.Cu,F.Paste,B.Paste,F.SilkS,B.SilkS,F.Mask,B.Mask,Edge.Cuts"


def fab(r: Recorder, tag: str, pcb: Path, out: Path, copper: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    layers = GERBER_LAYERS if copper == 4 else GERBER_LAYERS.replace("In1.Cu,In2.Cu,", "")
    t0 = time.monotonic()
    g = cli("pcb", "export", "gerbers", "-o", str(out) + "/", "--layers", layers, "--subtract-soldermask",
            "--no-netlist", "--check-zones", str(pcb), timeout=600)
    d = cli("pcb", "export", "drill", "-o", str(out) + "/", "--format", "excellon", "--excellon-separate-th",
            "--generate-map", "--map-format", "gerberx2", "-u", "mm", str(pcb), timeout=600)
    files = sorted(x.name for x in out.iterdir())
    r.check(f"{tag}.gerbers_drill", g.returncode == 0 and d.returncode == 0 and any(f.endswith(".drl") for f in files),
            f"{time.monotonic() - t0:.2f}s files={files}")
    r.check(f"{tag}.paste_layers", any("Paste" in f for f in files), [f for f in files if "Paste" in f])

    pos = out / "pos.csv"
    p = cli("pcb", "export", "pos", "-o", str(pos), "--format", "csv", "--units", "mm", "--side", "both",
            "--exclude-dnp", str(pcb))
    rows = list(csv.DictReader(pos.open())) if pos.exists() else []
    cpl = out / "CPL_jlcpcb.csv"
    with cpl.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
        for row in rows:
            w.writerow([row["Ref"], f"{float(row['PosX']):.4f}mm", f"{float(row['PosY']):.4f}mm",
                        "Top" if row["Side"] == "top" else "Bottom", f"{float(row['Rot']):.0f}"])
    r.check(f"{tag}.cpl", p.returncode == 0 and len(rows) > 0,
            f"{len(rows)} placements, columns={list(rows[0].keys()) if rows else None}; JLC CPL rows={len(rows)}")

    bom = out / "bom_raw.csv"
    b = cli("pcb", "export", "bom", "-o", str(bom), "--fields", "Value,Reference,Footprint,${QUANTITY},LCSC,MPN",
            "--labels", "Comment,Designator,Footprint,Qty,LCSC,MPN", "--group-by", "Value,Footprint",
            "--ref-range-delimiter", "", "--exclude-dnp", str(pcb))
    rows = list(csv.DictReader(io.StringIO(bom.read_text()))) if bom.exists() else []
    jbom = out / "BOM_jlcpcb.csv"
    with jbom.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Comment", "Designator", "Footprint", "JLCPCB Part #"])
        for row in rows:
            w.writerow([row["Comment"], row["Designator"], row["Footprint"].split(":")[-1], row.get("LCSC", "")])
    r.check(f"{tag}.bom", b.returncode == 0 and len(rows) > 0,
            f"{len(rows)} lines; with LCSC={sum(1 for x in rows if x.get('LCSC'))}; err={b.stderr[-200:]!r}")
    z = RESULTS / f"q08_{tag}_gerbers.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in out.iterdir():
            if f.suffix.lower() in (".gbr", ".drl", ".gbrjob") or f.name.endswith((".gtl", ".gbl", ".gto", ".gbo", ".gts",
                                                                                     ".gbs", ".gtp", ".gbp", ".gm1", ".g1", ".g2")):
                zf.write(f, f.name)
    r.note(f"{tag}.zip", f"{z.name} {z.stat().st_size} bytes, {len(zipfile.ZipFile(z).namelist())} files")
    for f in (cpl, jbom):
        (RESULTS / f"q08_{tag}_{f.name}").write_text(f.read_text())


def run() -> int:
    r = Recorder("q08_fab")
    base = Path(tempfile.mkdtemp(prefix="q08-"))
    for name in ("ecc83", "odh"):
        p, net = flow.prepare(base, name, None)
        with Server(p.pro) as srv:
            k = srv.client(600000)
            b, _, _ = flow.build(k, p, net, name)
            if name == "ecc83":
                freerouting.export_dsn_cli(p.pcb, p.dir / "b.dsn")
                freerouting.route(p.dir / "b.dsn", p.dir / "b.ses")
                freerouting.import_ses(b, p.dir / "b.ses")
                b.save()
        fab(r, name, p.pcb, p.dir / "fab", flow.SPEC[name][2])
    return r.finish()


if __name__ == "__main__":
    main_guard(run)
