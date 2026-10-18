"""Probe candidate stock symbols (pins, bbox) — sanity check for the scenarios."""

import sys
import time

from geom import symbol_bbox
from harness import Server, v2

import kit


def main():
    proj = kit.fresh_blank()
    with Server(proj) as srv:
        k = srv.client(timeout_ms=60000)
        t = time.monotonic()
        kit.load_libraries(k)
        print("load", round(time.monotonic() - t, 1))
        sch = k.get_schematic()
        for lib_id in sys.argv[1:]:
            c = sch.begin_commit()
            try:
                s = sch.place_symbol_from_library(lib_id, v2(100, 100))
                pins = [(p.number, p.name, int(p.electrical_type)) for p in s.definition.pins]
                print(
                    lib_id,
                    "ref",
                    s.reference_field.text.value,
                    "fp",
                    s.footprint_field.text.value,
                    "bbox",
                    symbol_bbox(s),
                    "pins",
                    pins[:60],
                )
            except Exception as e:
                print(lib_id, "ERR", e)
            finally:
                sch.drop_commit(c)


if __name__ == "__main__":
    main()
