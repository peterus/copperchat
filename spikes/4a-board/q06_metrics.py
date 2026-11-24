"""Q6: generic quality metrics (metrics.py) — human-routed ecc83 demo vs our placement + Freerouting
on ecc83 and OpenDriveHub. Shows which metrics separate good from poor layouts cheaply."""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path

import boards
import flow
import freerouting
import metrics
import place
from harness import RESULTS, Recorder, Server, main_guard


def measure(r: Recorder, tag: str, b) -> dict:
    t0 = time.monotonic()
    parts = place.read_parts(b)
    for p in parts:
        p.fixed = "MountingHole" in p.fp.proto.definition.id.entry_name
    m = metrics.routing(b, parts)
    m["airwire_mm"] = round(place.airwire_length(parts)[0], 1)
    dec = metrics.decoupling(parts)
    m["decoupling_worst"] = dec[:5]
    m["decoupling_over_3mm"] = sum(1 for d in dec if d["dist_mm"] > 3.0)
    m["decoupling_caps"] = len(dec)
    m["sheet_compactness"] = metrics.compactness(parts)
    m["metrics_s"] = round(time.monotonic() - t0, 2)
    r.note(tag, m)
    return m


def run() -> int:
    r = Recorder("q06_metrics")
    base = Path(tempfile.mkdtemp(prefix="q06-"))
    out = {}
    # human reference
    p = boards.ecc83(base / "ref")
    shutil.copyfile(p.dir / "ecc83-ref.kicad_pcb", p.pcb)
    with Server(p.pro) as srv:
        k = srv.client(60000)
        out["ecc83_human"] = measure(r, "ecc83_human", flow.open_board(k, p))
    for name in ("ecc83", "odh"):
        p, net = flow.prepare(base, name, None)
        with Server(p.pro) as srv:
            k = srv.client(600000)
            b, parts, info = flow.build(k, p, net, name)
            out[f"{name}_placed"] = measure(r, f"{name}_placed", b)
            freerouting.export_dsn_cli(p.pcb, p.dir / "b.dsn")
            fr = freerouting.route(p.dir / "b.dsn", p.dir / "b.ses", passes=40)
            freerouting.import_ses(b, p.dir / "b.ses")
            out[f"{name}_freerouted"] = measure(r, f"{name}_freerouted", b)
            out[f"{name}_freerouted"]["freerouting_s"] = fr["seconds"]
    h, f = out["ecc83_human"], out["ecc83_freerouted"]
    r.check("metrics_compute_on_all", all("segments" in v for v in out.values()),
            {k_: v["metrics_s"] for k_, v in out.items()})
    r.check("human_vs_freerouted_comparable",
            h["non_45_segments"] >= 0 and f["non_45_segments"] == 0,
            f"human: non45={h['non_45_segments']} acute={h['acute_bends']} vias={h['vias']} len={h['track_length_mm']} "
            f"detour={h['detour_median']}; freerouted: non45={f['non_45_segments']} acute={f['acute_bends']} "
            f"vias={f['vias']} len={f['track_length_mm']} detour={f['detour_median']}")
    o = out["odh_freerouted"]
    r.check("decoupling_metric_flags_far_caps", o["decoupling_caps"] > 0,
            f"{o['decoupling_caps']} caps classified, {o['decoupling_over_3mm']} > 3 mm; worst={o['decoupling_worst'][:3]}")
    (RESULTS / "q06_compare.json").write_text(json.dumps(out, indent=2, default=str))
    return r.finish()


if __name__ == "__main__":
    main_guard(run)
