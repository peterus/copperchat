import json, statistics, subprocess, time
from common import *
PAIRS = Path(os.environ["PAIRS"])
out = {}
for n in ("ecc83_edit", "hier_edit"):
    d = PAIRS / n; f = next((d / "before").glob("*.kicad_pro")).stem + ".kicad_sch"
    for fmt in ("json", "text", "png", "svg"):
        ts = []
        for _ in range(7):
            cmd = [KICAD_CLI, "sch", "diff", "--format", fmt, f"before/{f}", f"after/{f}"] + (["-o", f"/tmp/t.{fmt}"] if fmt in ("png", "svg") else [])
            t = time.time(); subprocess.run(cmd, cwd=d, env=ENV, capture_output=True); ts.append(time.time() - t)
        out[f"{n}.{fmt}"] = dict(median_s=round(statistics.median(ts), 3), min_s=round(min(ts), 3), max_s=round(max(ts), 3))
    ts = []
    for _ in range(7):
        t = time.time(); subprocess.run([KICAD_CLI, "sch", "diff", "--exit-code-only", f"before/{f}", f"after/{f}"], cwd=d, env=ENV); ts.append(time.time() - t)
    out[f"{n}.exit-code-only"] = dict(median_s=round(statistics.median(ts), 3))
print(json.dumps(out, indent=1))
json.dump(out, open("/w/spikes/2c-diff-highlight/results/timing.json", "w"), indent=1)
