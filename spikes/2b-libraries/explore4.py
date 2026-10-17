from harness import *
from kipy.geometry import Vector2
with Server(fresh_project()) as s:
    k=s.client(120000); s.open_schematic(); s.open_board()
    k.load_all_libraries(); k.wait_for_libraries(interval_ms=100)
    sch=k.get_schematic()
    n0=len(list(sch.get_symbols()))
    commit=sch.begin_commit() if hasattr(sch,"begin_commit") else k.begin_commit()
    try:
        inst=sch.place_symbol_from_library("MCU_ST_STM32F1:STM32F103C8Tx", Vector2.from_xy_mm(100,100))
        print("in commit: pins", len(inst.definition.pins), "fields", [f.name for f in inst.definition.fields][:6])
        n1=len(list(sch.get_symbols())); print("symbols during", n0, n1)
    finally:
        (sch.drop_commit if hasattr(sch,"drop_commit") else k.drop_commit)(commit)
    n2=len(list(sch.get_symbols())); print("after drop", n2)
