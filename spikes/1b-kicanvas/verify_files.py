"""Assert the files under files/ were upgraded by the nightly kicad-cli (header check only)."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("KICAD_DEMOS", Path.home() / "build/kicad/demos"))
PAIRS = [
    ("ecc83/ecc83-pp.kicad_sch", "ecc83/ecc83-pp.kicad_sch"),
    ("ecc83/ecc83-pp.kicad_pcb", "ecc83/ecc83-pp.kicad_pcb"),
    ("complex_hierarchy/complex_hierarchy.kicad_sch", "complex_hierarchy/complex_hierarchy.kicad_sch"),
    ("complex_hierarchy/ampli_ht.kicad_sch", "complex_hierarchy/ampli_ht.kicad_sch"),
    ("complex_hierarchy/complex_hierarchy.kicad_pcb", "complex_hierarchy/complex_hierarchy.kicad_pcb"),
]


def header(path: Path) -> tuple[int, str]:
    head = path.read_text(encoding="utf-8")[:400]
    version = int(re.search(r"\(version (\d+)\)", head).group(1))
    gen = re.search(r'\(generator_version "([^"]+)"\)', head)
    return version, gen.group(1) if gen else ""


def main() -> int:
    failed = 0
    for out_rel, src_rel in PAIRS:
        out, src = HERE / "files" / out_rel, SRC / src_rel
        if not out.exists():
            print(f"FAIL: {out_rel} missing")
            failed += 1
            continue
        v_out, g_out = header(out)
        v_src, _ = header(src)
        ok = v_out > v_src and (g_out.startswith("10.99") or g_out.startswith("11"))
        print(f"{'PASS' if ok else 'FAIL'}: {out_rel} version {v_src} -> {v_out} generator_version={g_out}")
        failed += 0 if ok else 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
