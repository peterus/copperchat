from harness import *
with Server(fresh_project()) as s:
    k=s.client(120000); s.open_schematic(); s.open_board()
    k.load_all_libraries(); k.wait_for_libraries(interval_ms=100)
    fp=k.get_library_footprint("Resistor_SMD:R_0805_2012Metric")
    p=fp.proto
    print([f.name for f in p.DESCRIPTOR.fields])
    q=type(p)(); q.CopyFrom(p)
    from kipy.util import unpack_any
    kinds={}
    for it in q.items:
        n=unpack_any(it).__class__.__name__; kinds[n]=kinds.get(n,0)+1
    print("items",len(q.items),kinds)
    del q.items[:]
    print(str(q)[:1800])
    print(fp.__class__.__name__, [a for a in dir(fp) if not a.startswith("_")])
    print("pads", len(fp.pads) if hasattr(fp,"pads") else None, "desc", getattr(fp,"description_field",None) and fp.description_field.text.value if hasattr(fp,"description_field") else None)
