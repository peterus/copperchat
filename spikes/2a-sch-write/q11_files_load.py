"""Q11: every project the spike saved through the API (results/files/*) loads in
kicad-cli: netlist export, SVG plot of all sheets, ERC. (KiCanvas: kicanvas/, Q12.)"""

import json
import tempfile
from pathlib import Path

from harness import FILES, Recorder, main_guard, require_kicad, root_sch, run_cli


def main() -> int:
    require_kicad()
    rec = Recorder("q11_files_load")
    projects = sorted(p for p in FILES.iterdir() if p.is_dir()) if FILES.exists() else []
    rec.check("artifacts_present", len(projects) >= 5, [p.name for p in projects])
    for proj in projects:
        tmp = Path(tempfile.mkdtemp(prefix=f"s2a-q11-{proj.name}-"))
        root = root_sch(proj)
        n = run_cli("sch", "export", "netlist", "-o", str(tmp / "n.net"), str(root))
        s = run_cli("sch", "export", "svg", "-e", "-o", str(tmp / "svg"), str(root))
        svgs = sorted((tmp / "svg").glob("*.svg"))
        e = run_cli("sch", "erc", "--format", "json", "--units", "mm", "-o", str(tmp / "erc.json"), str(root))
        ok_erc = e.returncode in (0, 5) and (tmp / "erc.json").exists()
        n_sheets = len(json.loads((tmp / "erc.json").read_text())["sheets"]) if ok_erc else 0
        rec.check(
            f"cli_loads_{proj.name}",
            n.returncode == 0 and s.returncode == 0 and ok_erc and len(svgs) == n_sheets >= 1,
            {
                "netlist_rc": n.returncode,
                "svg": [p.name for p in svgs],
                "erc_rc": e.returncode,
                "erc_sheets": n_sheets,
                "stderr": (n.stderr + s.stderr + e.stderr)[-200:],
            },
        )
    return rec.finish()


if __name__ == "__main__":
    main_guard(main)
