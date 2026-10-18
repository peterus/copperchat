"""Aggregates results/runs/*/result.json into results/summary.md (+ summary.json). Host-side,
no KiCad needed: python3 summarize.py"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent
RUNS = HERE / "results" / "runs"


def logical_changes(calls: list[dict]) -> int:
    """User-visible operations: a placed/moved/edited part, a connection, one pin given a
    label / power symbol / no-connect, one deleted part (vs. KiCad item changes)."""
    n = 0
    for c in calls:
        if not c["ok"] or c["item_changes"] == 0:
            continue
        a = c["args"]
        if c["tool"] in ("sch_add_label", "sch_add_power"):
            n += len(a["at"]) if isinstance(a.get("at"), list) else 1
        elif c["tool"] == "sch_no_connect":
            n += len(a.get("pins") or [])
        elif c["tool"] == "sch_delete":
            n += len(a.get("refs") or [])
        else:
            n += 1
    return n


def tokens(r: dict) -> int:
    u = r.get("usage") or {}
    return sum(u.get(k, 0) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))


def main() -> None:
    rows = []
    for f in sorted(RUNS.glob("*/result.json")):
        r = json.loads(f.read_text())
        sc = r["score"]
        rows.append(
            {
                "run": f.parent.name,
                "model": (r["model"] or "").removeprefix("claude-"),
                "tools": "v2.1" if "v2.1" in f.parent.name else f"v{r.get('tools_version', 1)}",
                "success": r["success"],
                "golden": f"{sum(sc['golden'].values())}/{len(sc['golden'])}",
                "erc_e": sc["erc_errors"],
                "erc_w": sc["erc_warnings"],
                "calls": r["tool_calls"],
                "errors": r["tool_errors"],
                "renders": r["renders"],
                "erc_runs": r["erc_calls"],
                "items": r["item_changes_total"],
                "logical": logical_changes(r["calls"]),
                "max_items_per_call": max(r["item_changes_per_call"] or [0]),
                "overlaps": len(sc["layout"]["symbol_overlaps"]),
                "wire_over_pin": len(sc["layout"]["wire_over_pin"]),
                "off_grid": len(sc["layout"]["off_grid"]),
                "not_0805": len(sc["passives_not_0805"]),
                "tokens": tokens(r),
                "out_tokens": (r.get("usage") or {}).get("output_tokens"),
                "cost": round(r["cost_usd"] or 0, 3),
                "wall_s": r["wall_s"],
            }
        )
    cols = list(rows[0]) if rows else []
    md = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    md += ["| " + " | ".join(str(r[c]) for c in cols) + " |" for r in rows]
    total = round(sum(r["cost"] for r in rows), 2)
    md += ["", f"Runs: {len(rows)}, successful: {sum(r['success'] for r in rows)}, total model cost: US${total}"]
    (HERE / "results" / "summary.md").write_text("\n".join(md) + "\n")
    (HERE / "results" / "summary.json").write_text(json.dumps(rows, indent=1))
    print("\n".join(md))


if __name__ == "__main__":
    main()
