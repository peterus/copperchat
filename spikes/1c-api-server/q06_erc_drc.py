"""Q6: kicad-cli sch erc / pcb drc --format json shape (LANG=C)."""

import json
import tempfile
from pathlib import Path

from harness import Recorder, fresh_fixture, main_guard, require_kicad, run_cli


def main() -> int:
    require_kicad()
    rec = Recorder("q06_erc_drc")
    proj = fresh_fixture("ecc83")
    out = Path(tempfile.mkdtemp(prefix="c1-rc-"))
    sch = next(proj.glob("*.kicad_sch"))
    pcb = next(proj.glob("*.kicad_pcb"))

    r = run_cli("sch", "erc", "--format", "json", "--units", "mm", "-o", str(out / "erc.json"),
                str(sch))
    rec.check("erc_runs", (out / "erc.json").exists(), f"rc={r.returncode} {r.stderr[-200:]}")
    erc = json.loads((out / "erc.json").read_text())
    rec.note("erc_top_keys", sorted(erc))
    rec.check("erc_sheets_violations", "sheets" in erc and all(
        "violations" in s for s in erc["sheets"]), sorted(erc))
    v = next((v for s in erc.get("sheets", []) for v in s["violations"]), None)
    if v:
        rec.note("erc_violation_sample", v)
        rec.check("erc_violation_keys", {"type", "severity", "description", "items"} <= set(v),
                  sorted(v))
        rec.check("erc_item_uuid_pos", {"uuid", "pos"} <= set(v["items"][0]), v["items"][0])
    rec.note("erc_violation_count", sum(len(s["violations"]) for s in erc.get("sheets", [])))
    rec.note("erc_sheet_keys", sorted(erc["sheets"][0]) if erc.get("sheets") else None)
    rec.note("erc_units", erc.get("coordinate_units"))

    r = run_cli("pcb", "drc", "--format", "json", "--units", "mm", "--schematic-parity",
                "-o", str(out / "drc.json"), str(pcb))
    rec.check("drc_runs", (out / "drc.json").exists(), f"rc={r.returncode} {r.stderr[-200:]}")
    drc = json.loads((out / "drc.json").read_text())
    rec.note("drc_top_keys", sorted(drc))
    rec.check("drc_keys", {"violations", "unconnected_items", "schematic_parity"} <= set(drc),
              sorted(drc))
    d = next(iter(drc.get("violations", []) + drc.get("unconnected_items", [])), None)
    if d:
        rec.note("drc_violation_sample", d)
        rec.check("drc_violation_keys", {"type", "severity", "description", "items"} <= set(d),
                  sorted(d))
    rec.note("drc_counts", {k: len(drc[k]) for k in ("violations", "unconnected_items",
                                                      "schematic_parity") if k in drc})
    rec.note("drc_units", drc.get("coordinate_units"))
    rec.check("english_descriptions", all(ord(c) < 128 for c in json.dumps(drc)[:5000]),
              "LANG=C → ASCII descriptions")

    # Exit-code semantics: default vs. --exit-code-violations
    rcs = {}
    for flag in ([], ["--exit-code-violations"]):
        r1 = run_cli("sch", "erc", "--format", "json", *flag, "-o", str(out / "e2.json"), str(sch))
        r2 = run_cli("pcb", "drc", "--format", "json", *flag, "-o", str(out / "d2.json"), str(pcb))
        rcs[" ".join(flag) or "default"] = {"erc": r1.returncode, "drc": r2.returncode}
    rec.note("exit_code_semantics", rcs)
    return rec.finish()


if __name__ == "__main__":
    main_guard(main)
