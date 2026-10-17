"""Run `kicad-cli sch diff` in all formats on each pair; record exit code, runtime, size."""
import json, subprocess, time
from common import *
PAIRS = Path(os.environ.get("PAIRS", "/tmp/c2/pairs"))
rows = []
for d in sorted(p for p in PAIRS.iterdir() if p.is_dir()):
    pro = next((d / "before").glob("*.kicad_pro")).stem
    f = f"{pro}.kicad_sch"
    for fmt in ("json", "text", "png", "svg"):
        out = d / f"diff.{fmt}"
        cmd = [KICAD_CLI, "sch", "diff", "--format", fmt, f"before/{f}", f"after/{f}"]
        if fmt in ("png", "svg"):
            cmd += ["-o", str(out)]
        t = time.time()
        r = subprocess.run(cmd, cwd=d, env=ENV, capture_output=True, text=True)
        dt = time.time() - t
        if fmt in ("json", "text"):
            out.write_text(r.stdout)
        (d / f"diff.{fmt}.err").write_text(r.stderr)
        rows.append(dict(pair=d.name, fmt=fmt, exit=r.returncode, secs=round(dt, 2),
                         bytes=out.stat().st_size if out.exists() else None, stderr=r.stderr.strip()[:200]))
        print(rows[-1])
json.dump(rows, open(PAIRS / "diff_runs.json", "w"), indent=1)
