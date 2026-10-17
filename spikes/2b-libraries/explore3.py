import time, collections
from harness import *
from kipy.proto.common.commands import library_commands_pb2, editor_commands_pb2
from kipy.proto.common.types import LibraryType
from kipy.proto.schematic import schematic_types_pb2
from kipy.geometry import Vector2
with Server(fresh_project()) as s:
    k=s.client(300000); s.open_schematic(); s.open_board()
    k.load_all_libraries(); k.wait_for_libraries(interval_ms=100)
    ids=list(k.get_library_symbols())
    t=time.monotonic(); defs={}
    for i in range(0,len(ids),500):
        c=library_commands_pb2.GetItemsFromLibrary(); c.type=LibraryType.LT_SYMBOL
        for x in ids[i:i+500]: c.item_ids.add().CopyFrom(x.proto)
        r=k._client.send(c, editor_commands_pb2.GetItemsResponse)
        for a in r.items:
            sy=schematic_types_pb2.SchematicSymbol(); a.Unpack(sy); defs[f"{sy.id.library_nickname}:{sy.id.entry_name}"]=sy
    print("fetch all", round(time.monotonic()-t,2), len(defs))
    noitems=[n for n,d in defs.items() if len(d.items)==0]
    print("no items", len(noitems), noitems[:10])
    nodesc=sum(1 for d in defs.values() if not d.description_field.text.text)
    nokw=sum(1 for d in defs.values() if not d.keywords)
    nofp=sum(1 for d in defs.values() if not d.footprint_field.text.text)
    nofilt=sum(1 for d in defs.values() if not d.footprint_filters)
    print("nodesc",nodesc,"nokw",nokw,"nofp default",nofp,"nofilters",nofilt)
    # derived: does placing give pins?
    sch=k.get_schematic()
    inst=sch.place_symbol_from_library("Amplifier_Operational:LM358", Vector2.from_xy_mm(100,100))
    d=inst.definition
    print("placed definition items",len(d.items),"pins",len(d.pins), "units", d.unit_count)
    # Which have extends? test a few: raw proto of LM358 hasn't parent
